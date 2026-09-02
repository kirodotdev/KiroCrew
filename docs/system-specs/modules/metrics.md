# Metrics Telemetry Module

## Overview

Local-first metrics telemetry built on the OpenTelemetry SDK (Apache-2.0 / CNCF).
The trunk is designed so future work is purely adding instrument calls at call
sites — never changing this plumbing. **Default OFF** (`telemetry.enabled:
false`): all metric call sites are cheap no-ops and nothing is written or
exported, byte-identical to no telemetry (mirrors the `mcp_gateway.enabled` /
`skills.lazy_load` opt-in convention). Any instrument that needs durable state to
survive a process death — currently only the session-lifetime breadcrumb — must
gate that state on the same consent, and fail closed when consent cannot be
read, so this sentence stays literally true.

Source: `src/kiro_crew/metrics/` — `schema.py`, `recorder.py`, `provider.py`,
`local_exporter.py`, `http_metrics.py`. Tests: `test/metrics/`.

## Components

| File | Purpose |
|------|---------|
| `schema.py` | Namespace constants (`NS_CORE = "kirocrew."`, `NS_GENAI = "gen_ai."`, `NS_APP_PREFIX = "app."`) + `validate_name` / `validate_attrs` / `redact` guardrails. Documents the low-cardinality contract. |
| `recorder.py` | `MetricsRecorder` — facade over the OTEL `Meter`. Every metric passes namespace + privacy guardrails BEFORE reaching an instrument. Instrument-cache creation is lock-guarded (atomic check-then-create). Best-effort: a telemetry failure never propagates to the caller. `meter=None` = no-op recorder. |
| `provider.py` | Consent gate + process-global recorder (`get_recorder()`) + graceful `shutdown()` / `reset_for_testing()`. `get_recorder()` serves a memoized recorder and re-resolves the `telemetry.enabled` consent value every `_CONSENT_RECHECK_SECS` (30s), rebuilding when it moved — see "Recorder lifecycle & threading" below. Public consent surface: `env_pin()` / `TELEMETRY_ENV_VAR`. When enabled, wires a `PeriodicExportingMetricReader` to the local JSONL exporter. Installs **one `View` per instrument** from `histogram_bounds()` (`_HISTOGRAM_BUCKETS_MS` for millisecond instruments plus `_HISTOGRAM_BUCKETS_BY_UNIT` for the non-ms ones), each with its own `ExplicitBucketHistogramAggregation` boundaries (see below) — deliberately NOT a catch-all `instrument_type=Histogram` View. |
| `local_exporter.py` | `JsonlMetricExporter` — appends one JSON line per export cycle to `<dir>/metrics-YYYY-MM-DD-<pid>.jsonl` (default dir `~/.kiro/crew/metrics`). Per-PID single-writer shards keep append + rotation lock-free, so concurrent exporters do not lose DELTA cycles. A private `.metrics.lock` serializes only retention sweeps; pruning skips canonical shards owned by live PIDs or modified within the safety window. **Bounded retention (rec #14):** shards rotate before an append exceeds `max_total_mb`; closed/expired shards are pruned directly by age and oldest-first size. Pruning is throttled to at most once per 300s and fully best-effort. Dir mode is 0o700, file mode 0o600, and nothing egresses the host. Declares DELTA `preferred_temporality` for Counter/UpDownCounter/Histogram so daily aggregation is an element-wise sum across cycles/PIDs; the map itself lives in `metrics/temporality.py`, which owns it for the OTLP leg too (declaring it inline here is what let that leg ship CUMULATIVE for the same instruments with nothing failing). Observable counters are deliberately NOT mapped and would export CUMULATIVE: the delta baseline lives in the provider, which is rebuilt in-process on a telemetry consent change, so DELTA would re-emit the process-lifetime total once per rebuild. Nothing registers an observable counter today — the process CPU/GC readings and the inventory probe-failure count are lifetime-total GAUGES, which is what removed the last cumulative series — so the entry guards the next one; the aggregator still reduces cumulative streams window-relative (deterministic identity boundary + time-ordered legacy reset detection + first-in-window baseline) for the shards written before that switch. **Process identity:** each record is stamped once at resource level with `kirocrew.process.start_time` (`schema.RESOURCE_ATTR_PROCESS_START_TIME`) — the writing process's OS start-time token from `platform_compat.own_process_start_time()`, module-cached so provider rebuilds inside one process stamp the SAME value, and reboot-unique (Linux start ticks + boot UUID; macOS microsecond `proc_pidinfo` instant; Windows creation FILETIME). A read that cannot honor one-token-one-process (unreadable boot UUID, no `libproc`, 1s-only sources) emits NO token rather than an aliasable coarse one — a degraded token would merge lifetimes AND mute the reset heuristic that catches merges. The shard-filename PID plus this token identify a process beyond PID reuse, making the aggregator's cumulative reset detection deterministic. The stamp lands on the serialized JSONL line, never on the SDK `Resource` — that `Resource` also feeds the opt-in OTLP reader, and this host-local token must not egress. Fail-soft: when the platform read is unavailable the field is absent and the aggregator's legacy value heuristic applies. Resource level, not a metric attribute, so it never multiplies series cardinality. |
| `temporality.py` | Single owner of the instrument-kind -> temporality map, for the local sink AND the opt-in OTLP leg. `delta_preference()` returns a fresh DELTA map for Counter/UpDownCounter/Histogram (ObservableCounter deliberately absent, gauges never applicable); `otlp_preference()` returns it, or `None` to stand aside when `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` is set. Exists because the map lived inline in `local_exporter.__init__` while `_build_otlp_reader` passed nothing, so the two destinations reported the same instruments DELTA on disk and CUMULATIVE on the wire with nothing failing. |
| `http_metrics.py` | Gateway HTTP observability (rec #1): `record_boot_to_ready()` (boot-to-ready histogram) + `make_route_latency_middleware()` (per-route latency, wired as the outermost middleware on both `start_dashboard`/`start_api_server`). Bounds `route_template` cardinality via `collect_route_templates()` (build-time snapshot) + `route_template()` (`__unknown__` fallback); clamps `method` to a fixed allowlist and `status_class` to `1xx`..`5xx`/`other`. Upgraded WebSocket connections and `text/event-stream` SSE responses are excluded because their handler elapsed time is connection/turn lifetime, not HTTP request latency. Best-effort — a telemetry failure never alters a response. |

## Recorder lifecycle & threading

`get_recorder()` returns a memoized recorder on a fast path guarded by a
monotonic clock (`_consent_recheck_due`): once a recorder exists it is handed back
directly until the recheck window elapses. Every `_CONSENT_RECHECK_SECS` (30s) the
call hands a consent check to a worker (`_schedule_consent_check_locked` ->
`_consent_worker`), which re-reads `telemetry.enabled` and rebuilds when it moved.
That is what makes `kirocrew config set telemetry.enabled true` — a write from a
SEPARATE process — take effect without a gateway restart. A caller that changed the
setting itself calls `shutdown()` to skip the wait. A config that cannot be READ
yields "no change" rather than `False`, so a transient read error never tears down a
working recorder; the worker stamps the recheck clock either way, so an unreadable
config cannot turn every metric call into a fresh file read.

**`get_recorder()` itself never reads config and never builds anything**, apart
from the very first build of the process, which has nothing to serve in the
meantime. `KiroCrewConfig.load()` is a fingerprint-cache hit in the steady state
(~0.3ms) but a full read plus schema validation when the file actually changed
(~14ms), and the rebuild costs ~57ms of SDK import — neither belongs on the event
loop, which the route-latency middleware drives on every HTTP request. The
consequence to know: the recheck is eventual in BOTH directions. A change is
noticed at the window boundary and lands a thread hop later, which is immaterial
against the window it already sits behind. `_check_in_flight` keeps a busy window
from spawning one worker per request, and is cleared in the worker's `finally` so a
crash costs one window rather than stranding the check.

Consent resolution is env-first: `env_pin()` reads `TELEMETRY_ENV_VAR`
(`KIROCREW_TELEMETRY`) and, when set, decides the effective state regardless of the
config flag; `_consent_enabled()` falls back to `telemetry.enabled`.

**`_lock` is never held across a provider or reader shutdown.**
`_take_provider_locked()` clears the globals under the lock and RETURNS the
provider; the flush happens after release. A provider shutdown joins each reader's
export thread (30s deadline) and, with `telemetry.otlp_endpoint` set, ends in a
synchronous network POST — holding the lock across it would stall `get_recorder()`
on lock ACQUISITION, and the route-latency middleware calls `get_recorder()` on the
event loop for every HTTP request. This applies to every path that reaches a
teardown, including the one that discards a superseded build.

**Which thread flushes depends on who dropped the recorder.** The consent worker
flushes on its own thread, after releasing the lock. `shutdown()` flushes on the
CALLER's thread, so process teardown and the config route (which calls it via
`asyncio.to_thread`) both observe completion — still not under the lock.
Partially-constructed readers from a failed init are reaped off-thread the same way
(`_reap_readers_detached`), because a reader shutdown performs a final export.

**`_build_recorder()` writes no module state.** It returns a `_Build` tuple
(recorder, provider, resolved consent) that a caller installs under the lock via
`_install_locked()`, so only the install is a critical section. Consent is recorded
alongside the recorder even on a host that can never record (OTel absent), because
leaving it unset would make every recheck window see a difference and rebuild a
no-op recorder in a loop.

**A `_build_generation` counter makes a mid-build disable stick.** Each rebuild
captures the counter and rechecks it before installing; `_take_provider_locked()`
bumps it, so a disable that lands while a build is in flight is not undone by the
build finishing afterwards — the superseded build's provider is flushed (outside the
lock) and discarded instead. Every consent change starts its own worker rather than
skipping when one is already running: skipping would leave the recorded consent
updated while the recorder stayed a no-op, and the next recheck would then find no
difference and never retry.

**Only the FIRST build of a process is synchronous** (`_ever_built` is still
False), because there is no recorder to serve in the meantime and that path is not
a steady-state request path. Every later build is a REbuild and goes off-thread,
including one after `shutdown()` cleared the state — otherwise the config route's
own write would put the SDK import back on the event loop. `reset_for_testing()`
clears `_ever_built`, so a test's next build is synchronous and assertable without
polling. `shutdown()` bumps the generation but does not stop an already-running
consent worker, so `reset_for_testing()` also waits (bounded, `_RESET_WAIT_BOUND_SECS`)
for `_check_in_flight` to clear before returning — raising if the bound expires —
so a worker left running by an earlier test can never mutate module state
underneath the next one.

One consequence of the detached flush: while it is still in its join, a re-enable
can put a second exporter on the same per-PID shard, which the local exporter's
single-writer assumption does not cover. Both sides swallow their IO errors, so the
worst case is one dropped export cycle rather than a corrupt shard.

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
| `enabled` | `false` | Main switch. Off = no-op recorder, nothing written. Editable from the dashboard (Settings → Privacy) as well as the config file, `kirocrew config set`, and the env var; re-resolved live, so a change takes effect without a restart. |
| `local_dir` | `""` | JSONL shard dir; empty = `~/.kiro/crew/metrics`. `~` expansion supported. |
| `export_interval_seconds` | `60` | Flush interval (floored to 1). |
| `retention_days` | `0` | Age pruning is disabled by default to preserve pre-existing history on upgrade. Set a positive day window to opt in (rec #14). |
| `max_total_mb` | `0` | Size pruning is disabled by default to preserve pre-existing history on upgrade. Set a positive opportunistic directory budget to opt in; protected active writers can temporarily exceed it (rec #14). |
| `otlp_endpoint` | `""` | Opt-in OTLP/HTTP metrics endpoint (e.g. `http://localhost:4318/v1/metrics`). **Empty = no network egress (default).** When set, aggregated metrics are ALSO pushed to this collector in addition to the local JSONL sink; requires `pip install "opentelemetry-exporter-otlp-proto-http==1.44.0"` (rec #1). |

Field validation (`TelemetryConfig.__post_init__`): `export_interval_seconds`
below 1 is floored to 1; negative `retention_days` / `max_total_mb` are clamped
to `0` (cap disabled) rather than being interpreted as "prune everything".

## Opt-in, retention bounds & egress (rec #14 / rec #1)

**Default posture — nothing collected, nothing leaves the host.**
`telemetry.enabled` defaults `false`, so every metric call site is a cheap no-op
and no file is written — including the session-lifetime breadcrumb, which is
gated on the same consent and fails closed. Even once local collection is
enabled, `otlp_endpoint` defaults empty, so **no data ever leaves the machine
unless the operator explicitly sets an OTLP endpoint.**

**Easy opt-in (four equivalent ways):**
- **Config flag:** set `"telemetry": {"enabled": true}` in `~/.kiro/crew/config.json`.
- **CLI:** `kirocrew config set telemetry.enabled true`.
- **Dashboard:** the recording switch in Settings → Privacy, which writes the same
  key through `PATCH /api/config/kirocrew` (`telemetry.enabled` is in
  `_EDITABLE_CONFIG`). That route refuses `true` with **HTTP 409** when
  `telemetry.otlp_endpoint` is set: `_build_recorder` attaches an OTLP reader
  whenever the active telemetry provider supplies a destination — which the public
  default does for any non-empty endpoint — so enabling from a switch offered as
  local-only would start network egress. The endpoint is chosen in the config
  file, so that is where enabling on such a host is done. Disabling is always
  allowed — a narrower local choice always composes. An unreadable config also
  fails closed with a 409. On a successful write the route calls
  `provider.shutdown()` via `asyncio.to_thread`, so the value applies on the very
  next metric rather than at the next recheck.
- **Env var:** export `KIROCREW_TELEMETRY=1` (also accepts `true`/`yes`/`on`;
  `0`/`false`/`no`/`off` force-disables). The env var overrides the config flag
  and is handy for CI / containers / one-off debugging. It gates **local
  collection only** — it never enables network egress. Resolved by the public
  `provider.env_pin()` and consumed by `provider._consent_enabled()`.

None of these requires a gateway restart: `get_recorder()` re-resolves consent
every `_CONSENT_RECHECK_SECS` (30s) and rebuilds when it moved (see "Recorder
lifecycle & threading"). The gateway process is where the session/turn/HTTP
metrics are recorded; other kirocrew processes pick the value up on their own
recheck or at their next start.

**External OTLP egress (opt-in, off by default):** egress destinations are
**pluggable**. `_build_recorder` asks the active `TelemetryProvider` for them via
`otlp_destinations(cfg)` and attaches one `PeriodicExportingMetricReader` per
returned destination that names the `"metrics"` signal (`provider._otlp_destinations`
resolves, `provider._build_otlp_reader` builds one reader). The public
`DefaultTelemetryProvider` turns a non-empty `otlp_endpoint` into exactly one
destination and returns none when it is empty, so a standalone build behaves as it
did when this module constructed that exporter itself. Readers are appended AFTER
the local JSONL sink, so an edition can add destinations but can never remove or
replace the sink the dashboard reads, and it supplies no cadence — the export
interval stays `telemetry.export_interval_seconds`. Nor does it supply a
TEMPORALITY: `_build_otlp_reader` passes the same DELTA map the local sink uses
(`metrics.temporality.delta_preference`, the single owner for both sinks), so one
`MeterProvider`'s two destinations cannot describe the same instrument two
different ways. It passes NOTHING when
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` is set, leaving that variable
in full control: the exporter applies an explicit dict ON TOP of whichever base
the variable chose (`OTLPMetricExporterMixin._get_temporality` in the
`otlp-proto-common` package — the reader lives there, not in the SDK tree or the
http exporter tree), so passing the map unconditionally would silently override an
operator who asked for CUMULATIVE.

A destination carries `name` (a non-secret label used in logs, because the
endpoint value never is), `endpoint`, `signals`, and optionally an
authenticated `session`. The session is why the seam exists: `requests`
re-evaluates `Session.auth` per request, so a credential that rotates during
process lifetime (an OIDC/SSO id_token, STS credentials) is re-read on every
export, where `OTEL_EXPORTER_OTLP_HEADERS` — the only injection point before this
seam — freezes into the exporter's session at construction and starts returning
401 once it rotates. Static per-destination headers ride on `Session.headers` and
need no field of their own.

The method must be cheap and side-effect-free per call: it is read once per
recorder build AND on every egress-posture read (the Privacy panel's status, and
each `telemetry.enabled` config write), so an edition must build its transport
once rather than acquiring a credential inside it.

Filtering is deny-by-default: a destination with an empty `endpoint`, or one aimed
at a signal this core does not emit, is dropped rather than trusted. A provider
that raises contributes nothing and is reported at WARNING, and telemetry keeps
working local-only; the same is true when the platform context cannot be composed,
because for an egress seam "no destinations" is the closed state.

Install support with `pip install "opentelemetry-exporter-otlp-proto-http==1.44.0"`. If a destination is supplied
but the package extra is
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
when extra missing, plus `TestConsentRecheck`: out-of-band enable/disable, the
rebuild and the flush both off the calling thread, a mid-rebuild disable not
undone by the build, the hot path not reading config every call, an unreadable
config keeping the live recorder, `shutdown()` applying a change without waiting
out the window and without holding the lock across the flush, and the env pin
still winning after a config edit), `test/test_collection_status_endpoint.py`
(`GET /api/telemetry/collection`: effective state, env/overlay pins, endpoint
presence without the endpoint string),
`test/metrics/test_schema.py` (redaction / namespace).

### Resource attributes: what identifies a series (and what egresses)

The SDK `Resource` is the payload contract for both sinks: it labels every
series on the local JSONL line AND, when `otlp_endpoint` is set, everything
pushed to that collector. `_resource_attributes()` in `provider.py` is the
single place it is built, and every value there is either a closed set or an
explicit clamp, because a resource attribute is a label on EVERY series this
process exports — one unbounded value there multiplies every instrument.

| Attribute | Value | Bound |
|-----------|-------|-------|
| `service.name` | `kirocrew` | constant |
| `service.instance.id` | the persisted anonymous install id (`beacon.install_id`) | one per install |
| `process.pid` | `os.getpid()` | one per live process |
| `service.version` | build version, release-clamped via `beacon.release` | `major.minor.patch`, no build stamp |
| `os.type` | `linux` / `darwin` / `windows` | CLOSED, else `other` |
| `host.arch` | `amd64` / `arm64` / `x86` | CLOSED, else `other` |
| `process.runtime.name` | `cpython` / `pypy` / `jython` / `ironpython` | CLOSED, else `other` |
| `process.runtime.version` | `beacon.python_minor()` | `major.minor`, never the patch |
| `host.cpu.logical_count` | `os.cpu_count()` | small integer |

Two identities, deliberately separate. `service.instance.id` counts DEVICES: a
random UUID persisted on disk, never derived from hostname or username (which
on a corporate desktop routinely embed the employee's alias), and stable across
restarts so "this install over time" survives them. `process.pid` counts
PROCESSES: one install runs several telemetry-enabled processes at once (the
gateway plus spawned agents/apps each build their own recorder), and with an
install-scoped resource alone their gauges and cumulative counters would
interleave into one corrupted series at a backend. Collapsing the two would
report one machine's 6-8 processes as 6-8 machines.

**Why the install id may egress while `kirocrew.process.start_time` may not.**
The start-time token is a host-local process fingerprint that exists only to
make the aggregator's cumulative-reset detection deterministic; it has no
fleet-level question to answer, so it is stamped on the JSONL line and kept off
the `Resource` (see `local_exporter.py` above). The install id is the opposite:
it is the only thing that can answer "how many distinct devices", it is random
rather than derived, and it is the SAME id the beacon already sends. Note that
omitting the key does not yield an unlabelled resource — the SDK substitutes
its own per-process uuid4, i.e. a fresh series set per restart — so the choice
is between a stable anonymous id and per-restart churn, not between an id and
no id.

The id is MINTED in `_build_recorder()`'s live branch, immediately before the
resource is assembled, so the first export already carries it and no startup
window can leak the SDK's substitute. That branch runs only under consent True,
so an install with telemetry off creates nothing. Consequence to know: enabling
metrics is what creates the beacon's install-id file, even on a host that has
the beacon itself disabled.

Deliberately ABSENT: the distribution channel (the beacon's data-minimization
pass removed its channel field, because channel sharply narrows the crowd a
stable id hides in — stamping it on every metric payload would quietly undo
that decision) and an install type (no reliable detection today; a guessed
label would be confidently wrong). Both are guarded by a regression test.

Tests: `test/metrics/test_resource_attrs.py`.

## Instrumented signals

| Metric | Type | Attrs | Site |
|--------|------|-------|------|
| `kirocrew.session.startup.duration` | histogram (ms) | `outcome` (`ready` / `auth_required` / `error`), `spawned` (bool), `backend` (`kiro`) + `phase` (`total` / `spawn_init` / `session_new` / `session_load` / `set_model`), `channel` (conversation source), `resumed` (bool) on the kiro path | Two sites. **claude**: `acp/client.py::AcpClient.ensure_ready()` — times cold-start (spawn + session init) and emits in a `finally` so every exit path is measured, with no `phase` attr. **kiro** (default): `providers/acp.py::_emit_kiro_startup_metric` — one `phase=total` point PLUS one point per internal phase; `spawned` is unconditionally `True` because `_start_kiro_runtime_impl` always spawns a fresh runtime (the warm fast-path returns before reaching either site and is NOT measured). `outcome` defaults to `"error"` so an unexpected exception is never mislabeled `"ready"`. Consumers MUST treat only the end-to-end point (`phase` absent or `total`) as a startup — the phase points are components of one startup. `channel` comes from `messaging.link::telemetry_channel_of`, a closed label set (an unrecognised key classifies as `other`, never the key itself) answering WHICH surface paid the cost; `resumed` separates the `session/load` path from `session/new`. `session_load` is recorded only when a resume was attempted, and `session_new` only when `create_session` actually ran, so a resumed startup never reports a near-zero `session_new`. |
| `kirocrew.session.pool.decision` | counter | `outcome` (`hit` / `miss_empty` / `bypass_resume` / `bypass_stateless` / `bypass_cwd` / `bypass_effort` / `bypass_env` / `disabled` / `other`), `channel` | `session.py::SessionManager._record_pool_decision`, one point per `get_or_create` warm-pool decision. Exactly one reason is reported per decision — the disqualifiers form a disjunction, so branch order picks the reported reason, not the outcome. Deliberately a counter rather than an attribute on the startup histogram: "was the pool used" and "how long did startup take" are separate questions, and crossing them would multiply every phase series. `bypass_resume` quantifies how often a `resume_sid` disqualifies a session from the pool. Values are pinned by `session.POOL_DECISIONS`. |
| `kirocrew.session.resume.outcome` | counter | `outcome` (`loaded` / `fallback_replay` / `no_session_file`), `channel` | `providers/acp.py::_emit_kiro_startup_metric`, emitted only when a resume was attempted. Distinguishes a lossless native resume from the degraded fallback (fresh `session/new` plus history replay on the Kiro Crew side, taken when `session/load` exhausts `_RESUME_MAX_ATTEMPTS` against a stale lock) and from a resume skipped because the session file was gone. |
| `kirocrew.session.duration` | histogram (ms) | `end_reason` (`reset` / `removed` / `unclaimed` / `destroyed` / `discarded` / `shutdown` / `retired` / `recycled` / `evicted` / `crashed`), `session_source` (via `messaging.link::telemetry_channel_of`) | `metrics/sessions.py::record_session_ended` (or `record_sessions_ended` for a path that drains many keys at once) for every clean end; `metrics/sessions.py::backfill_crashed_sessions` for `crashed`. **Why the reason is passed in, never derived.** There is no single teardown funnel: `session_lifecycle.py` holds six end paths that each pop the registry themselves (`reset` / `remove` / `remove_if_unclaimed` / `destroy` / `discard_conversation` / `close_all`), and four more pop it outside all of those — `retire_kiro_identity_sessions` reports `retired` for the sessions it tears down on an identity change, `reload_provider_factory` reports `retired` too for the registry it clears on a provider switch (both are "this process is no longer the right host for these sessions", which is why they share a label rather than minting a second one), `session_compaction.py::CompactionCoordinator._recycle_held` reports `recycled` (context overflow replacing the provider in place), `session_background.py::recycle_heartbeat` also reports `recycled` (the background runtime replacing its own session at cycle end), and the two dead-provider evictions in `session_allocation.py` report `evicted`. So each site states its own `end_reason`, the same contract `metrics/turns.py` holds its callers to. Every path calls this while it still holds the session registry lock, in the same event-loop tick as its `_sessions.pop`: the pop and the histogram record are both in-memory and both happen before the call's single suspension point, and a later call lets a racing cold start register a SUCCESSOR under the reused key whose record the predecessor's teardown then consumes. `reset` is a PATH label rather than a cause — the idle sweep and a slot reset both reach teardown through it, and the finer causes are already counted by `kirocrew.session.idle_expired` and `kirocrew.watchdog.recovery.outcome`; the other four are separate precisely because they never take the `reset` route. **EVERY registry removal must record an end, and that is a correctness requirement rather than a completeness one.** An unrecorded removal does not merely lose a sample: the crumb below survives it, so the next boot reports the session as `crashed` — a failure that never happened, injected into the one population this histogram exists to measure. Four such paths shipped unrecorded and were caught in review, which is why the gate for this is `test_every_registry_removal_records_an_end`, a fail-closed AST walk over every module that mutates the registry (recognising `pop`, `del` and `clear`) rather than a hand-kept list of method names that a new path silently escapes. **The crumb is what makes a crash countable.** A start time held only in memory dies with the process, which is exactly the population most worth measuring, because a crash runs no teardown path at all. So `record_session_started` drops one small JSON file under `<data home>/metrics/open-sessions/`, named by SHA-256 digest of the session key PLUS the writing pid and the start time, with the key stored inside (keys carry channel ids and slot names and are not filesystem-safe; `session_map.json` already holds every key in the same data home, so this is not new exposure). **The name identifies the writer and the generation, not just the key, and that is what makes every unlink here safe.** A session key is not unique across processes — `BACKGROUND_KEY` is a fixed constant, so a `kirocrew run` and the gateway hold the same key at the same time with a live session each — so while the name was the digest alone they shared one file, and whichever ended first unlinked the other's record; the locks in this module are THREAD locks and never spanned processes, so nothing here could have prevented it. With the writer and the generation in the name, a crumb can only ever be removed by the session that wrote it. Three consequences follow. A clean end unlinks the ONE generation it can name, and an end with no live table entry unlinks NOTHING — the files sharing that digest belong to other processes or earlier runs, and removing them is precisely the bug the name now prevents. A start that displaces a live generation of the same key, and an eviction under `_MAX_LIVE_SESSIONS`, must each reap the generation they orphan, because once the table forgets a generation nothing can ever name its file again; both ride the same awaited worker hop as the start's own write, so those unlinks stay off the event loop without opening a second cancellation window. And the backfill legitimately finds SEVERAL files for one key, each a distinct session judged on its own recorded owner. Whatever survives to the next boot is a session that never ended cleanly, which `backfill_crashed_sessions(started_before)` emits as `crashed` and unlinks; it is also the only reader of these files, so an unparseable crumb is reaped there rather than by an end unlinking blind. `started_before` is the calling process's own start time, so a crumb at or after it is left alone. **The crumb also records WHO owns it — the writing process's pid plus a per-process start identifier — and the backfill skips any crumb whose owner is still running.** The cutoff alone was not enough: it only ever protected THIS process's own crumbs, and `cli.py` and `eval/runner.py` each build their own session manager against the same data home, so a `kirocrew run` session started before the gateway booted was read as a crash. That both invented a sample and deleted the live crumb, losing the real crash that session might later suffer. The start identifier is what makes pid reuse safe: a recycled pid belongs to a different process and must not look like the owner, and that includes reuse of the CURRENT process's own pid — a container hands the gateway PID 1 on every restart, so a crashed predecessor's crumb arrives carrying this process's pid, and skipping it on the strength of the pid alone meant its crash was never emitted and its file never cleaned on any later boot either. **Liveness comes from `platform_compat.pid_exists`; the start identifier only refines it.** `get_process_start_id` returns None on Windows and for any process the caller may not introspect, and its own contract says a None must NOT be read as a mismatch — using it as the liveness test judged every owner dead on that entire platform and reaped live sibling sessions there. The ownership check fails CLOSED — a live pid whose identity cannot be read counts as running, so an ambiguous crumb waits for a later boot rather than becoming a fabricated crash; losing a real crash sample costs one data point, inventing one corrupts the population this instrument reports. A crumb with no ownership recorded (written before this field existed) falls back to the cutoff alone. **A registration that never became a session is discarded, not ended:** `discard_session_start` pops the table entry and unlinks that generation's crumb WITHOUT emitting, for the cancellation window between inserting a session into the registry and finishing its start record — a session that never lived has no lifetime to report, and it must not be left as a crumb the next boot calls a crash. It is deliberately not a twelfth `end_reason`, because those describe how a live session ENDED. **Exactly-once accounting.** A crumb is consumed at most once, so the teardown paths — which are NOT mutually exclusive, the idle sweep calls `reset` — cannot double-count between them, and a backfilled session cannot be counted again on the boot after. The clean path deliberately reads neither the crumb nor the popped entry's `created_at`: consuming a DIFFERENT record than the crash path would let a clean end's leftover crumb be re-emitted as `crashed`, and `created_at` is reset when a provider is recycled in place, so it measures provider age rather than registry residency. **A crashed session's END time** is its transcript mtime (`<data home>/sessions/<stem>.jsonl`, appended as the conversation runs, so it is the last moment the session was observably alive and the honest post-hoc maximum). A session with no transcript — a subagent leaves only a replay log — yields no end time and is unlinked WITHOUT an emit rather than recorded as a 0ms lifetime: absence reads as "no data", a recorded zero renders as a real zero. **Two locks, split by what they may be held across.** `_live_lock` guards the in-memory start table ONLY and is never held across I/O, because every end takes it on the event loop; `_crumb_io_lock` orders the two worker-thread paths that touch crumb FILES — the writer, and the backfill's read-decide-unlink — and nothing on the loop may take it. Coupling them was a real stall: the backfill held one lock across a crumb read plus an unlink on a worker while a teardown waited for that same lock on the loop. The end path needs no crumb lock at all, because ownership already partitions the work — the backfill skips every crumb whose owner is live, so the crumbs a session ends in THIS process and the crumbs the backfill reaps are disjoint sets. It is also worth stating that these are THREAD locks and so never protected anything against a sibling PROCESS; that is exactly what the ownership field is for. The write itself runs on a worker thread (`asyncio.to_thread`) and is AWAITED while the caller still holds the session registry lock, which is what satisfies two constraints that look opposed: the syscalls never run on the event loop, where a slow or network-homed data home would stall every gateway task behind one session insertion, and nothing can interleave, because starts, ends and successors all serialise on that registry lock. Three earlier shapes each satisfied one and broke the other — a fire-and-forget pool write let a late writer delete a successor's crumb (an ended-key set, then a generation token compared before and after the write, both failed, because every check still acted on a path the successor SHARED), and writing inline fixed the ordering by putting filesystem I/O on the loop. Naming the file after its writer and generation is what finally removed that shared path, so the post-write check now removes only this writer's own file. The END path's unlink runs on a worker thread too, and is AWAITED there for the same reason the write is (#7537). Inline on the event loop it was one syscall against the data home on the paths a person is waiting on -- closing a tab, resetting a slot, shutting the gateway down -- so on a slow or network-backed home that single `unlink` parked every gateway task behind whichever session happened to be closing, and the stall was felt as the whole gateway freezing rather than as a metrics problem. Queued fire-and-forget it would be worse than slow: `shutdown_maintenance_executor` drains with `cancel_futures=True`, so at `close_all` -- when teardown is heaviest -- the unlink is dropped outright and a cleanly ended session's crumb survives to be back-filled as `crashed`, inflating the exact population this instrument measures. Awaiting a worker hop is the only shape that is both off the loop and never dropped. The same-tick rule the inline version protected is unaffected, on three counts: the registry pop and the histogram sample both happen BEFORE the function's only suspension point, so a teardown path still accounts for its session immediately after its `_sessions.pop`; every caller awaits while it still holds the session registry lock, which is the lock a racing cold start must take to register and map a SUCCESSOR; and the hop is awaited so nothing is fired blind. **Submitted is not started, so the hop is a BARE executor future awaited through `asyncio.shield`.** On a saturated executor the work item is still queued, and cancelling the await reaches the underlying `concurrent.futures.Future` through `futures._chain_future`'s `_call_check_cancel`, which cancels a work item that has not begun -- the same dropped-unlink failure that ruled out the maintenance pool, reached by a different queue. Shielding leaves the inner future alone, so nothing cancels the queued work and it runs when a thread frees up. Two details make abandoning it safe on the cancellation path, and both are why this is not the fire-and-forget shape the start path forbids. It is a Future and NOT a Task, so `runners._cancel_all_tasks` at loop teardown -- which iterates `all_tasks()` -- cannot reach it; shielding a `to_thread` COROUTINE would be wrong, because `shield` wraps a coroutine in a Task that teardown cancels, dropping a still-queued item all over again. And a worker finishing after the loop closed cannot raise "Event loop is closed", because `_chain_future._call_set_state` returns early when `dest_loop.is_closed()` instead of calling `call_soon_threadsafe`; the historical Windows failure was a start-path write never awaited at all, whereas here the normal path awaits to completion and only a cancellation abandons the future. Unlinking inline in the cancellation handler was tried and rejected in review: it fixed the drop but put a filesystem syscall back on the event loop, which is the thing this issue is about. The start path still must not use this shape, because its write has to be ORDERED under the caller's registry lock and so cannot be allowed to land late; an end's unlink can, because the crumb name encodes the writer and the generation, so a late unlink can only ever reach the file its own session wrote. If the executor is already shut down the hop cannot be scheduled at all, and the crumb is left to the boot backfill rather than unlinked on the loop -- reachable only once the loop itself is tearing down, and the backfill's ownership check means it is claimed only after this process is gone. **A cancellation at that hop is ABSORBED by all three end paths, and that is what makes the new suspension point safe to give a teardown.** The registry pop is the caller's point of no return: past it only the caller's remaining cleanup can finish the job, and `destroy` deletes the session-map entry in a `finally` BELOW its end call while `discard_conversation` and `reset` clear the resume SID on the line after the lock. Raising would exit those callers before that cleanup, leaving a permanently destroyed session's mapping behind for the next boot to resume -- a data-loss window the synchronous version did not have, because it had no suspension point there at all. A cancellation cannot undo a teardown, only leave it half done, so the correct answer is to let the teardown finish. That cancellation is genuinely DISCARDED rather than deferred -- a swallowed `CancelledError` is not re-delivered by asyncio, so the caller runs to completion unless something cancels it again -- which is the intended outcome rather than a side effect, because finishing a teardown that has already popped is exactly what the callers' `finally` blocks exist for. The consequence worth knowing is that a caller wanting a hard cap on teardown must impose it outside the pop, since a cancellation delivered after the pop is not honoured; no caller does today (every `close_all` await site is bare). The two guarantees sit in two places deliberately: whether a CALLER may be aborted is knowable only where its post-pop cleanup is known, so it lives in the end paths, while whether the UNLINK survives a cancelled hop is a property of the hop, so it lives in the helper. Two more consequences are worth stating. The sample is emitted BEFORE the hop, the opposite of `kirocrew.session.started`'s counter, because the risks are opposite: a cancelled start is rolled back and never existed, while an end cannot be rolled back (the pop has already consumed the only record of that generation), so emitting first is what keeps the sample under cancellation and what measures the lifetime at the pop rather than a worker round-trip later. And the four paths that pop MANY keys at once -- `close_all`, `drain_all_providers`, `retire_kiro_identity_sessions` and `reload_provider_factory` -- call `record_sessions_ended` instead, which pops the whole set under one `_live_lock` hold and unlinks it in ONE hop: a hop per key would put a cancellation point BETWEEN two keys, leaving every key after it popped but unrecorded, and on `close_all` (a path cancellation reaches by design) that would turn one orderly shutdown into a burst of fabricated crashes -- strictly worse than the on-loop unlink it replaced. `discard_session_start` is a coroutine for the same reason, and absorbs its cancellation for a second one too: its callers re-raise the failure that started the rollback on the very next line, so letting a cancellation escape would displace that failure with one of its own making. **The crumb is written only under telemetry consent**, checked fail-closed: it exists solely to feed this instrument, which is a no-op without consent, so writing one on an unopted host would persist state nothing can read against a documented default of collecting nothing. Bounded throughout: `_MAX_BACKFILL_EMITS` (2000) caps a runaway crumb directory while still unlinking everything it walks, and `_MAX_LIVE_SESSIONS` (4096) drops the oldest half of the table, unlinking each dropped generation's crumb as it goes — both cost samples, never memory. Own bucket family `_SESSION_BUCKETS_MS`. |
| `kirocrew.session.started` | counter | `session_source` (via `messaging.link::telemetry_channel_of`) | `metrics/sessions.py::record_session_started`, one increment per session admitted to the registry, called from the sites that insert into `_sessions` (`session_allocation.py` cold-start and resume paths, `session_background.py`). The denominator the `kirocrew.session.duration` population is a subset of: not every registry removal records an end, so `started` counts admissions while `duration` counts the endings actually observed, and the gap between the two is itself readable. `session_source` is the SAME `telemetry_channel_of` derivation the duration histogram and `metrics/turns.py` use, so all three group by one bounded label set (an unrecognised key classifies as `other`, never the key itself). The increment and the `_live_starts` write are in-memory and therefore safe under the session registry lock; only the crumb touches disk, off-thread. |
| `kirocrew.turn.duration` | histogram (ms) | `outcome` (`ok` / `timeout` / `cancelled` / `tool_stall` / `stale_recover` / `stall_exhausted` / `error` / `unclassified`), `session_source` (via `messaging.link::telemetry_channel_of`), `model` + `provider` (omitted when the caller cannot resolve them) | **Two emit owners, exactly one sample per turn.** (1) `dashboard/handlers/usage.py::persist_token_record_async` — the call EVERY dispatch surface already makes once per turn at EVENT_COMPLETE, which is what makes cron, the heartbeat, memory consolidation, subagents, task-runner steps, workflow stages and the messaging channels present in this metric at all; before that they emitted nothing and the page reported the interactive median as the system's health. It reuses the built row's own `duration_ms`, so the row store and the histogram cannot disagree about one turn. (2) `dashboard/chat_runner.py::_emit_turn_metric` in `_run_chat`, which passes `emit_metric=False` to its persist call and emits itself — that persist sits behind `usage_has_billing`, so a turn that timed out having billed nothing writes no row and would otherwise lose its sample, and only this surface holds the EFFECTIVE session key (a linked channel conversation is attributed to its channel, not to the `slot.key` the row is filed under) and the spent-recovery-budget knowledge that labels `stall_exhausted`. `metrics/turns.py::turn_outcome` owns the mapping (`""`/`None`/`end_turn`/`stop`/`completed` → ok; the two watchdog stop reasons map to their own outcomes — checked BEFORE the `timeout` substring — so a recovered stall is never counted as a generic fault and the stall population stays visible; a stall arriving with its 3-attempt recovery budget already spent, or on a NESTED turn (`_prompt_depth > 0`, which the recovery branches never re-queue — it dies with "please retry"), labels `stall_exhausted`, which IS a terminal fault to the aggregator, so the recovered-stall exclusion cannot hide a session that dies needing user action). `cancelled` is a user pressing Stop, matched by EXACT equality against `STOP_REASON_CANCELLED` so the watchdog's `"error: cancel unacked"` still reaches the error branch; it used to fold into `error`, which put every deliberate cancel into `fault_rate`'s numerator, and it is now excluded from the numerator while REMAINING in the denominator (the turn ran). `unclassified` marks a turn whose surface passed a bare `TurnUsage` and so had no stop reason to give; it is in neither the numerator nor the denominator of `fault_rate` (in the numerator it would invent a fault for every clean background turn; in the denominator alone it would dilute the rate towards zero as background traffic grows), and its count is readable as `outcome["unclassified"]`. One histogram powers turn latency p50/p90 AND fault rate. The value is `duration_ms or elapsed_ms`: the acp provider always reports `TurnUsage.duration_ms == 0` (only claude_code fills it), so the caller must pass the locally measured wall clock as `elapsed_ms` or nothing is ever emitted. A still-zero value skips the emit deliberately — absence renders as "no data", whereas a recorded 0 would render as a plausible 0ms p50. **What it measures:** the wall clock starts at turn start, so a turn parked on an interactive tool-approval prompt counts operator thinking time. No finer-grained source exists on the acp path, so this is "turn wall-clock", not pure model latency — a high p90 can mean slow approvals rather than a slow model. |
| `kirocrew.turn.tokens` | counter (token) | `direction` (`input` / `output`), `model` + `provider` | Same two owners and the same exactly-once split as `kirocrew.turn.duration`, emitted from `metrics/turns.py::emit_turn_usage` off the same built row, so volume and latency describe one population. ONE instrument with a `direction` attribute rather than an `.input`/`.output` pair: the two series then carry an identical attribute set by construction (a `model` added to one cannot be forgotten on the other), the dashboard's counter path already reports every attribute combination under `by_attr`, and `direction` is a two-value enum so the cardinality cost is exactly 2x. **Positive-only.** Each backend fills only the dimensions it bills in; the kiro/acp backend reports zero for every token field, so emitting the zeros would publish a full series of them per turn and a recorded 0 reads as a measured zero rather than as "this backend does not report here". A negative value is also dropped — a monotonic counter cannot take it back. |
| `kirocrew.turn.credits` | histogram (credit) | `model` + `provider` | The turn's billed amount on a credit-billing backend (kiro/acp). Emitted only when non-zero, which is what makes it mutually exclusive with `cost_usd` on a given host (`acp/types.py`: "Consumers read whichever is non-zero"). Two instruments rather than one with a `currency` attribute because credits and dollars are not the same quantity: percentiles over the union have no unit. Own bucket family `_CREDIT_BUCKETS` (0.025–2500), calibrated against 17,240 real per-turn rows (min 0.03, p10 0.076, p50 6.8, p90 53, p99 155, max 658). NOT in `_HISTOGRAM_BUCKETS_MS`, and claimed by name in `_aggregate` so it never reaches the `*_ms` surface — see the bucket-boundaries section. |
| `kirocrew.turn.cost_usd` | histogram (usd) | `model` + `provider` | The same amount on a dollar-billing backend (claude_code / bedrock), same non-zero gate, same owners. Own bucket family `_USD_BUCKETS` (0.001–100). This is the one bucket array with no local calibration — a credit-billing host reports zero for every `cost_usd` row by construction — so it is sized from published per-token pricing against the observed token range and is worth re-checking once a dollar-billing host reports. Reported through `_amount_stats`, which keeps six decimals so a sub-cent turn does not round to `0.0`. |
| `kirocrew.tool.call.duration` | histogram (ms) | `tool_kind` (`read` / `fetch` / `search` / `edit` / `write` / `create` / `delete` / `move` / `execute` / `think` / `switch_mode` / `client_built_in` / `mcp` / `other`), `outcome` (`completed` / `failed` / `cancelled`) | **Round-trip latency of one tool call, keyed by kind and never by tool name.** The tool NAME is deliberately not an attribute: MCP names are unbounded (any installed server contributes its own) and the recorder caches one instrument per distinct attribute value with no eviction, so a name-valued label is a cardinality bomb. `tool_kind` is the ACP `kind` normalised by `metrics/tool_calls.py::classify_tool_kind` and pinned to the closed `TOOL_KINDS` set; the `kind` field arrives verbatim from the agent, so an unrecognised or agent-authored kind — including an empty one — folds to `other`, which is what stops a tool name minting a series. An MCP-served call is labelled `mcp` whatever kind it reported, decided by the trusted `_meta.kiro.mcpServerName` rather than the reported kind: an MCP server's kind vocabulary is its own, and "this call left the process over MCP" is the more useful fact than a builtin's finer verb. `outcome` carries exactly the three terminal ACP statuses — a non-terminal update (`pending` / `in_progress` / absent) records nothing and leaves the clock running, so no fourth value is reachable. A `<= 0` span skips the emit, the same rule `kirocrew.turn.duration` follows. The clock is `time.perf_counter`, NOT `time.monotonic`: on Windows the latter advances in ~15.6ms ticks, so any call completing inside one tick measured exactly 0.0 and was dropped by that guard — which silently hid every sub-tick tool call on the platform, i.e. most cached reads. `perf_counter` is the highest-resolution monotonic clock available everywhere, so the guard keeps meaning "unmeasurable" rather than "fast". **Site: two parser layers, one registry, one sample per call.** There is no single choke point, because the two backends parse through different code: the shared builders in `acp/_dispatch.py` (`_build_tool_call_event` starts the clock once the trusted MCP identity is resolved; `_build_tool_result_event` stamps the terminal status BEFORE its output parse can return None, so an output-less completion is still measured), where the scope arrives as `cache_scope` forwarded by `acp/session_handle.py` as the frame's `sessionId`; and `acp/client.py::AcpClient._extract_tool_call_update` with its result-extracting sibling, which shape the same frames inline and scope on the client's own `_session_id`. Every other surface consumes the emitted `AcpEvent` stream downstream of these two, so instrumenting both parsers covers all of them. The registry is keyed `scope|tool_call_id`, spelled exactly as `_dispatch`'s own tool-input cache key: a backend-assigned `toolCallId` is unique only WITHIN one backend session while one runtime hosts many, so the scope is what stops two sessions reusing an id from colliding, and a finish in the wrong scope never steals another session's entry. Both layers derive one frame's scope from the same session id, so whichever sees a frame second pops the entry the first opened and the call is sampled once rather than twice — the disjoint-parser assumption failing would then be a harmless miss instead of a double count. A start is idempotent per scoped id (the first start time and kind win, so a `tool_call_update` refinement cannot shrink the span), a finish with no matching start emits nothing, and the registry is bounded (`_MAX_OPEN_CALLS`, dropped wholesale on overflow: missing samples for in-flight calls, never a leak). The watchdog's `acp/liveness.py::ToolCallState.dispatch_ts` is deliberately NOT reused — it is a single last-tool-wins slot, right for stall attribution but wrong for a histogram because interleaved calls overwrite each other, and it exists on only one of the two paths. Own bucket family `_TOOL_CALL_BUCKETS_MS`. Best-effort throughout: a tool call never fails because its telemetry did. |
| `kirocrew.watchdog.action` | counter | `action` (`deferral` / `probe` / `cancel`), `verdict` (`working` / `dead` / `unknown` / `stuck_input`), `evidence_class` (`established_flat` / `mcp_flat` / `shell` / `shell_absent` / `wait` / `degraded`), `window` (`narrowed` / `extended` / `standard`), `agent_override` (bool) | `acp/session_handle.py::AcpSessionHandle._emit_watchdog_metric`, one point per watchdog DECISION in `_dispatch_events`: `deferral` from `_log_working_deferral` (rides its 10-min rate limit, so an hours-long WORKING build contributes a bounded handful of points, not one per tick), `probe` at the stale-probe send, `cancel` before `_end_stalled_tool`. `evidence_class` is `_watchdog_evidence_class` — a prefix/shape bucket of the free-form oracle evidence (pids/deltas/commands never emitted). `window` encodes the effective window selection: `narrowed` = a tool-branch evidence TAG reduced the suspect window below the build-scale default (1h) — `established_flat` to the model-silent budget (minutes), `shell_absent` to the ordinary silence budget (`stale_window_secs`, 300s) because the shell command has no process to its name; `extended` = model-wait-branch `established_flat` extended the stale window from 300s (`stale_window_secs`) to 900s (`model_silent_probe_secs`) for a non-streamed server-side think; `standard` = ordinary window in all other cases. `agent_override` is the per-agent watchdog-override BOOLEAN from the `WatchdogSettings` snapshot — deliberately NOT the agent name (free-form ⇒ cardinality bomb; per-agent joins happen via the row store's `agent` + `stop_reason` fields below). Guardrail query: `action=cancel, evidence_class=mcp_flat, window=standard` must not increase — the narrowed window may only affect `established_flat` and `shell_absent`. |
| `kirocrew.watchdog.idle.duration` | histogram (ms) | `action`, `evidence_class` | Same emit helper, same decision points; value = the branch's idle clock (`_tool_idle` / `_stale_idle`) at decision time, converted to ms at the emit site because the dashboard's generic aggregation reports every histogram under `*_ms` keys (a seconds instrument would render 1000x off). Answers whether 900s is right for LLM-shaped stalls (idle-at-action distribution per evidence class). Own bucket family `_WATCHDOG_IDLE_BUCKETS_MS` (1s–4h, densest at the 300/900/3600-second window boundaries). |
| `kirocrew.watchdog.recovery.outcome` | counter | `mechanism` (`stale_recover` / `tool_stall`), `outcome` (`recovered` / `exhausted`), `attempt_bucket` (1–3) | `dashboard/chat_runner.py::_emit_recovery_outcome`, derived from the per-slot retry budgets the stop-reason branches maintain (`slot._stale_recovery_retries` / `slot._tool_stall_retries`). `exhausted` emits in the stall branches when a budget hits its cap ("start a new chat"); `recovered` emits at the budget-reset block when a turn completes with outcome `ok` while a stall budget is armed — the stall branches return early, so an armed budget reaching that reset is by construction a completed recovery cycle (gated on `ok` so a user cancel of the recovery turn never counts as a recovery). `attempt_bucket` clamps to the 3-attempt cap (closed enum, mirrors the CLI's `attempt_number_bucket`). Every `recovered` point is one prevented hang. Fault accounting for the exhausted case lives on the turn histogram, not here: the final turn of an exhausted cycle labels `stall_exhausted` (see `kirocrew.turn.duration`), so a dead session counts toward `fault_rate` while this counter stays pure mechanism telemetry. |
| `kirocrew.context.section.duration` | histogram (ms) | `section` (one fixed label per assembled block: `preamble` / `profile` / `workspace` / `docs` / `steering` / `thread_history` / `stop_notes` / `memory` / `skills` / `lessons` / `provenance` / `finalize`, plus `episodic` from the `build_message` site), `custom` (bool) | Two sites, both first-turn only. `context.py::ContextBuilder.build_session_context` emits one point per section from monotonic checkpoints taken as each block is appended; `context.py::ContextBuilder.build_message` emits `section=episodic` for the query-dependent episodic retrieval that runs as that method's sibling rather than one of its sections. **Why per-section:** the block is assembled AFTER the user's message arrives and the caller awaits it before dispatching the prompt, so every section lands directly on time-to-first-token; as one opaque interval the cost is unattributable and diagnosis degrades to guess-and-rebuild. The spread within a single build is the widest of any instrument here — string appends under a millisecond alongside a query-embedding section reaching seconds — which is why it takes `_FAST_BUCKETS_MS` (0.5ms..60s) rather than a startup ladder. `custom` is a bool rather than the agent name deliberately: a populated install has dozens of agents, and one series per agent per section would multiply series count for no diagnostic gain. Sections under 1ms are still recorded as points but omitted from the companion INFO line to keep it readable. |
| `kirocrew.mcp.backend.acquire.duration` | histogram (ms) | `warm` (bool — `not was_spawned`) | `mcp_gateway/gatewayd.py::_emit_backend_acquire_metric` — ensure_backend pre-flight + lazy-spawn paths; acquire-only duration captured before attach_stub/create_task overhead. |
| `kirocrew.mcp.lazy_load.count` / `.duration` | counter + histogram (ms) | `transport` (`stdio`) | `mcp_gateway/gatewayd.py::_emit_lazy_load_metrics` — legacy lazy-spawn path (also emits backend.acquire). |
| `kirocrew.mcp.warm_pool.acquire` | counter | `result` (`hit` / `miss`) | `mcp_gateway/prewarm.py::HotKeyStore.record_outcome` (emitted outside the lock). |
| `kirocrew.skill.lazy_load.count` / `.duration` | counter + histogram (ms) | `hit` (bool) | `skills.py::SkillsLoader.load_skill` via `_emit_lazy_load_metric` (best-effort; never breaks skill loading). |
| `kirocrew.subagent.spawned` | counter | `concurrency` (int — the live running count INCLUDING this spawn, bounded by the manager's own concurrency cap, so a MAX over it is the high-water mark without a second instrument), `batched` (bool — the spawn is a member of a fan-out wave rather than a lone one) | `subagent_manager/admission.py::SpawnAdmissionCoordinator._log_spawned_impl` — the confirmed-start funnel, reached only after a spawn is admitted, so a rejected or never-started spawn is not counted (a promise an admission-time increment could not make). The `emit_counter` import is function-local, not module-level: `bind_component_globals` rebinds each `*_impl`'s `__globals__` to the owning namespace, where a module-level import here would not be visible. |
| `kirocrew.cron.fires` | counter | `kind` (`agent` / `script` / `command` — the dispatch shape; `script` and `command` bypass the model entirely, so this is the split between a job that costs tokens and one that costs none), `trigger` (`scheduled` / `manual`) | `cron.py::CronService._run_job_isolated`, one increment per execution, emitted BEFORE the jitter sleep so a run cancelled during jitter still counts as fired. No job id, name, message or schedule string reaches the attributes. |
| `kirocrew.artifact.created` | counter | `kind` (the closed `ALLOWED_KINDS` set), `source` (the closed `ALLOWED_SOURCES` set), `kind_auto` (bool — the kind was inferred from the content rather than pinned by the caller) | `artifacts.py::ArtifactStore.create`, emitted after the write and its change notification, so a failed create contributes nothing. `kind` and `source` are exactly the values `_validate_kind` / `_validate_source` already close over, so no slug or free-form label can enter the series. |
| `kirocrew.workflow.runs` | counter | `authored` (bool — the run writes its own script from an intent instead of executing supplied source), `replay` (bool — a restart-subtree reusing cached agent results rather than re-calling the model) | `workflows/runner.py::WorkflowRunner.run`, the one place every run passes before executing anything, foreground or background (the background entry point drives this same method). Both attributes are booleans over run mode; no intent text, script or run id is emitted. |
| `kirocrew.context.compactions` | counter | `success` (bool — whether the compaction ATTEMPT itself completed) | `session_compaction.py::CompactionCoordinator._fire_compact_callback`, one point per compaction that reached a verdict. Placed on this method rather than inside the callback because every compaction passes through here whether or not a surface registered a compact callback, and the early return below would otherwise drop exactly the surfaces that register none. The sum is compaction ATTEMPTS. `success` is deliberately NOT an effectiveness measure: the coordinator learns whether the backend finished the compaction, not how much context it actually freed, and no before/after context reading is taken, so a compaction can complete while reclaiming little. **The counter's `success` is also not the callback's.** `_recycle_held` fires this funnel with `success=True` because the SESSION now has headroom — which is what the callback needs to know — but it is reached exactly when an in-place `/compact` FAILED and the provider had to be replaced instead. Counting that as a successful compaction would report the failure mode as the success case, so the counter reports `False` there. The recycling marker is set for the whole of `_recycle_held` and popped only after this call, which is what separates the two populations at this point; `test_a_failed_compact_that_recycles_is_not_counted_successful` pins that ordering. |
| `kirocrew.mcp.reconnects` | counter | `pool` (bool — the reconnecting stub was a warm-pool bridge rather than a directly owned one) | `mcp_gateway/stub.py::_reconnect`, one increment per stub reconnect to a restarted gateway daemon, emitted after the replayed handshake is delivered. Each increment is a bridge that died and was rebuilt under a live session, so a rising rate is daemon instability that sessions are absorbing silently. The daemon detail string and the per-session reconnect ordinal go to the log line only, never the attributes. |
| `kirocrew.approval.decisions` | counter | `decision` (`allow` / `auto_approve` / `deny` — the gate's own bounded action constant), `security_deny` (bool — separates a hard security refusal from a policy-state deny) | `hooks.py::ToolHookResult._count`, called by the four result factories and nowhere else, so it fires exactly once per gate consultation. A surface that OVERRIDES the gate constructs a result directly (the chat runner downgrading an auto-approve to an interactive card, for instance) and is deliberately NOT counted: counting every construction would report one request as two decisions and keep a count for a verdict that was discarded. Counting from the factories rather than from `__post_init__` behind a flag is what removes the flag — it had no reader but the counter, so it was state carried purely to signal. Both attributes are bounded by construction — no deny reason, tool name or command reaches the recorder. |
| `kirocrew.gateway.boot.duration` | histogram (ms) | `server` (`dashboard` / `api`), `outcome` (`ready`) | `dashboard/server.py::start_dashboard` / `start_api_server` — boot-to-ready: wall-clock from the server's `start_time` until full init completes and it is about to accept traffic. Emitted via `metrics/http_metrics.py::record_boot_to_ready`. Best-effort; never blocks startup. |
| `kirocrew.gateway.request.duration` | histogram (ms) | `method` (fixed HTTP-verb allowlist, else `OTHER`), `route_template` (matched aiohttp canonical TEMPLATE, e.g. `/api/artifacts/{slug}`, else `__unknown__`), `status_class` (`1xx`..`5xx` / `other`) | `metrics/http_metrics.py::make_route_latency_middleware` — outermost gateway middleware on BOTH `start_dashboard` and `start_api_server`. Times full in-gateway HTTP handling; upgraded WebSocket connections and `text/event-stream` SSE responses are excluded so connection/turn lifetime cannot pollute request latency. **Bounded cardinality** (see below). |

| `kirocrew.process.threads.python` | gauge | — | `metrics/process_gauges.py::register_process_gauges`, callbacks run only at reader collection (no polling threads). `threading.active_count()`. |
| `kirocrew.process.threads.os` | gauge | — | Same module; `platform_compat.process_thread_count(os.getpid())` — OS-level count that catches native pools (ggml, grpc) invisible to `threading`. Linux-only; None elsewhere (gap, not zero). |
| `kirocrew.process.open_fds` | gauge | — | Same module; delegates to `platform_compat.count_open_fds` (shared with gatewayd's zombie-diagnostic `fd_count`): `/proc/self/fd` or `/dev/fd` entry count minus the enumeration fd; Windows reports the kernel handle count (platform-dependent semantics). |
| `kirocrew.process.memory.rss_bytes` / `.peak_rss_bytes` | gauge (By) | — | Same module; delegate to `platform_compat.proc_rss_bytes` (current) / `proc_peak_rss_bytes` (high-water mark), both cross-platform. A 0 return maps to None: gap, never a fake zero sample. |
| `kirocrew.process.cpu.seconds` | gauge (s) | — | Same module; `platform_compat.proc_cpu_seconds` process-lifetime user+system CPU. An observable GAUGE, not a counter: as counters these four were the only CUMULATIVE series exported, and one cumulative series forces every consumer into whole-hour stateful aggregation. Listed in `process_gauges.LIFETIME_TOTAL_METRICS`, which the dashboard aggregator reads to reduce them window-relative rather than reporting a lifetime total as a current reading. A 0 return maps to None: gap, never a fake zero (which the reducer would take as a new baseline). |
| `kirocrew.process.gc.collections` / `.collected` / `.uncollectable` | gauge | `generation` (`0`/`1`/`2`) | Same module; `gc.get_stats()` per generation, process-lifetime totals reported as gauges (see the CPU row). Rules GC in/out of a leak diagnosis (rising uncollectable = reference cycles; flat collected with rising RSS = native leak). |
| `kirocrew.inventory.crons.active` | gauge | — | `metrics/inventory_gauges.py::register_inventory_gauges`, callbacks run only at reader collection. The enabled-count reduction is `cron.enabled_count_from_disk`, a module-level sibling of `unhealthy_jobs_from_disk` that returns `(count, loadable)` and is the single owner of that loop — `CronService.count_enabled_from_disk` calls it too and keeps only the count, so the probe cannot drift from what the scheduler considers enabled, and neither needs a running gateway. Deliberately NOT `list_jobs`: that re-arms the asyncio timer, which from a reader thread with no running loop raises AND cancels the live timer first, stopping every scheduled job. A missing store is a genuine `0` and no fault (a fresh install has no crons). A store that is present but unparseable yields a gap **and increments `probe.failures{probe="crons"}`** — `_read_job_records` calls that case a fault, and a silent gap would be indistinguishable from a host that stopped exporting, which is the confusion the counter exists to resolve. |
| `kirocrew.inventory.monitor_loops.active` | gauge | — | Same module; `autonudge.get_instance()` then a materialized `list(...)` snapshot of the loop registry counting `active`. The registry is mutated only under an `asyncio.Lock` on the event loop, which gives a reader thread no protection and cannot be acquired from it; the snapshot is safe anyway because each dict op is atomic under the GIL and a gauge stale by one loop is fine. `get_instance()` returning None (spawned agent process, unit test, auto-nudge off) yields a gap — a `0` there would be indistinguishable from a running service with none armed. |
| `kirocrew.inventory.skills.installed` | gauge | — | Same module; one module-global `SkillsLoader(install_builtins=False)` (`install_builtins=True` would make a metrics probe perform a content-hashing write sync). Listing is a recursive walk, so it is cached for `_EXPENSIVE_TTL_SECS` (300s) — five collection cycles on the default interval. One combined total, NOT a builtin/user split: builtins are copied onto disk into the same tree, so nothing in the listing distinguishes them and a split would be a guess. |
| `kirocrew.inventory.memory.migrated` | gauge | — | Same module; `memory.migrated` as 0/1. Reports the migration bit rather than a "memory enabled" switch because no such switch exists — the embedding provider is coerced to a real value on load, so memory is structurally always on and an "enabled" gauge could only read 1. The bit flips on the first boot that initialises vector memory, fresh installs included, so 1 is the healthy reading and a **0 on a live install is a fault**: vector memory never initialised, migration aborted before the flip, or the flag write was skipped on an unparseable `config.json`. That is what keeps it from being a rollout metric that goes quiet once a fleet converges. |
| `kirocrew.inventory.knowledge.documents` | gauge | — | Same module; the raw source count. Deliberately NOT a constant 1 under a magnitude-band attribute: the quasi-identifying argument for banding does not hold when the only destination is the operator's own collector describing their own machines, while the band costs a series per band, hides growth inside a band (the drift the gauge exists for), and fixes the boundaries at emit time — a backend can band a raw count at query time and cannot recover a count from a band. Counts the `sources` table, not `items` (an item is one chunk, so counting items would report a chunking artifact as a document count), via a **read-only** `sqlite3` URI guarded on `exists()`: constructing a `KnowledgeStore` would run schema init plus graph load and would CREATE the database on an install that never ingested anything. Cached 300s. Absent database yields a gap. |
| `kirocrew.inventory.lessons` | gauge | — | Same module; the raw lesson count, same shape and rationale. Reads through one module-global `LessonStore`, whose own mtime cache makes a repeated read on an unchanged file a single `stat` — reused across ticks because that cache lives on the instance, and left uncached here because a second cache would add only staleness. An absent file is a genuine `0` (an install that never saved a lesson), unlike the knowledge database. |
| `kirocrew.inventory.mcp.servers` | gauge | `class` (`first_party` / `third_party`) | Same module; the roster merged from the agent config and `_load_mcp_json_by_source`, classified against `mcp_discovery._MANAGED_SERVER_NAMES` — reused rather than restated, since a second name list here would drift silently the first time a managed server is added or renamed. **Server names are read to classify and then discarded: no name ever reaches an attribute.** A roster is user-chosen free-form text, so publishing it would be both a cardinality bomb and a disclosure of what the user has installed. An empty merged roster yields a gap: the loaders fail soft to `{}`, so empty cannot be told apart from an unwritten config, and the managed servers are always present once installed. |
| `kirocrew.inventory.config.toggle` | gauge | `key` (closed enum, `inventory_gauges.CONFIG_TOGGLES`) | Same module; each declared feature switch as 0/1. Config is read ONCE for the whole set, so the gauge costs one fingerprint-cached load per collection instead of one per key. `telemetry.enabled` is deliberately absent — this module only runs inside the consent gate, so it could report nothing but 1. `memory.migrated` is absent because it has its own gauge. A key whose field cannot be resolved is OMITTED rather than reported as 0 (a renamed config field must read as a missing series, never as a switch someone turned off); `test_every_declared_toggle_resolves_against_a_real_config` is what keeps that from being a silent hole. |

| `kirocrew.inventory.probe.failures` | gauge | `probe` (closed enum, `inventory_gauges.ALL_PROBES`) | Same module; times a probe raised, per probe. **Absent on a healthy install** — it publishes only once something has failed, so a healthy host adds no series, and roster assertions must exempt it (`_HEALTHY_ABSENT` in `test_otlp_wire_e2e.py`). It exists because absence has TWO readings that this family cannot afford to conflate: a missing series can mean the probe broke OR that the host stopped exporting, and distinguishing a drifted host from a dark host is the job these gauges were added to do. A broken probe therefore presents as data — the failure series is present and climbing, which a host that went dark cannot produce. The first failure per probe also logs at WARNING (a default install's journal keeps WARNING and above, so debug would be invisible on exactly the installs that need it); repeats drop to debug so a permanently broken probe cannot emit one WARNING per export interval for the process lifetime. An observable GAUGE whose reading is a process-lifetime total, declared in `inventory_gauges.LIFETIME_TOTAL_METRICS` (see the process CPU/GC rows): as an observable counter it was the last CUMULATIVE series exported, and one is enough to force every consumer into whole-hour stateful aggregation. |

All eighteen registrations are wired in `provider.py::_build_recorder` (live path
only) and wrapped so a gauge failure can never disable telemetry as a whole. The
two families register in SEPARATE `try` blocks — they read entirely different
subsystems, and the process gauges must not go dark because, say, the skills tree
is unreadable. Each reader callback is individually guarded — a failing probe
yields a gap for that cycle, never an exporter error.

Because the inventory probes read subsystems rather than process counters, cost is
the binding constraint: a callback runs once per export interval **per reader**, so
an install with an OTLP destination configured enters them on two ticker threads
concurrently. Every probe is O(1) in-memory, a single small file parse, or
explicitly TTL-cached (`_ttl_cached`, whose produce call runs OUTSIDE its lock so a
second reader misses the cache rather than blocking on the first). A `None`
reading is cached like any other answer, so an install that can never answer does
not re-pay an expensive failing probe every cycle.

Scope of the `kirocrew.inventory.*` family, stated here because the names invite
the opposite reading: it is OPERATOR-fleet telemetry, not project-wide adoption
analytics. Collection is off by default, egress is a second opt-in, and OTLP needs
the `kirocrew[otlp]` extra, so any aggregate describes one operator's opted-in
machines. Adoption data structurally cannot ride this trunk — see "Why it is NOT
part of the OTEL trunk" above, whose four reasons apply to these instruments
exactly as they do to the beacon's payload. Do not later "promote" these to answer
install-population questions.

**One publisher per install (`mark_install_reporter`).** `_build_recorder` runs in
every process that touches telemetry, and an install runs several at once — the
gateway, `gatewayd`, spawned agents and apps (which is why the local exporter
shards per PID). That is correct for process-scoped instruments and WRONG for
install-scoped ones: each process would publish the same "this install has 14
crons" under its own resource, so an aggregate over installs counts one install
once per running process, and the bucketed gauges (whose shape is "publish 1, sum
by bucket") would make one install look like N. Deduplicating downstream is not
available, because that needs an install-scoped resource identity and the resource
carries only a per-process id today. So exactly one process publishes: the gateway
calls `inventory_gauges.mark_install_reporter()` during startup, and every callback
checks it at COLLECT time (not at registration), so it does not matter whether a
recorder was built before or after the claim. A process that never claims it
registers the same instruments and publishes nothing. The election is an EXPLICIT
call rather than inferred from, say, `autonudge.get_instance()` being non-None
(true only in the gateway today) — an inference stops holding the moment that
service's startup moves or becomes conditional, and the failure mode is inventory
silently disappearing.

That failure mode applies to the explicit call too, which is why the call SITE is
part of the contract: it sits in `GatewayOrchestrator.run()` at a point no branch
guards. A feature-flagged host does the same damage an inference would — the claim
first shipped inside `_init_autonudge()`, which returns early under
`KIROCREW_AUTONUDGE=0`, so one env var left no process publishing inventory at all.
Nothing reported it, either: the callbacks return before probing, so `probe.failures`
stays empty and the whole subsystem reads like a host that stopped exporting.
`test_reporter_claim_is_unconditional` parses the gateway and asserts the call is
reached from `run()` without passing through a conditional (`try` is allowed —
telemetry must never block boot).

A persisted install-scoped resource id is the precondition for ever removing this
mechanism, but it is not by itself a replacement. With one, an install CAN be
deduplicated downstream (`max by (install)` over its processes) — but that is
query-time discipline: the exported data would still contain one copy per process,
and a naively written fleet `sum` would read N times the truth. The election makes
the data correct at the point of emit, which no consumer has to remember. So
retiring it is a deliberate trade (fewer moving parts here, a rule every dashboard
must follow there), not something that falls out of the identity work landing.

Scope of the guarantee: it elects one GATEWAY PROCESS, which is one install wherever
the product runs one gateway per data home. Two gateways sharing a data home on
different ports would both claim, because the dashboard's singleton is per port, not
per data home — a pre-existing property of that configuration that duplicates every
install-scoped fact, and one no flag can detect (a second gateway double-counts
whether or not it runs crons). A pod is not this case: it boots with its own
`KIROCREW_HOME` and copies no crons, sessions, or databases, so its inventory belongs
to a different install and publishing it is correct.

### The counters beside `stats.py`, and why they are not the same numbers

`stats.py`'s in-memory singleton already counts adjacent ground: `sessions_created`,
`subagents_spawned` and its tool-approval trio, all consumed by one dashboard
system handler. Those tallies are NOT merged into the counters above, and the
counters are not derived from them. The singleton is a process-local count that
resets on restart and answers "what has this gateway done since it came up"; the
instruments above are time-series carrying attributes, survive a restart, and
answer "what is the rate, broken down by shape". Merging them would mean giving
the singleton attributes and durability it has no consumer for.

The hazard the split creates is that the names are near-identical while the
POPULATIONS differ, so the two numbers can legitimately disagree and be read as a
bug. `kirocrew.approval.decisions` is the sharpest case: it counts gate VERDICTS,
every consultation including the auto-approves and denies that never surface to a
person, whereas the singleton's approval trio accrues on the surface where a human
acted. `kirocrew.subagent.spawned` fires on the confirmed-start funnel after
admission, so it excludes rejected spawns a request-time tally would include.
Each row above therefore states which population its counter measures; a
comparison across the two systems is only meaningful once those definitions are
read.

### Histogram bucket boundaries: per instrument, not shared

Boundaries live in two metric-name → boundaries maps in
`metrics/provider.py` — `_HISTOGRAM_BUCKETS_MS` for millisecond instruments and
`_HISTOGRAM_BUCKETS_BY_UNIT` for everything else — merged by
`histogram_bounds()`, from which one `View(instrument_name=…)` is built per
instrument. Eight families, each sized to its instrument's measured range:

| Family | Range | Unit | Instruments |
|---|---|---|---|
| `_FAST_BUCKETS_MS` | 0.5ms – 60s | ms | `gateway.request`, `mcp.backend.acquire`, `skill.lazy_load`, `context.section`, `telegram.api`, `embed.queue_wait`, `embed.inference` |
| `_STARTUP_BUCKETS_MS` | 1ms – 60s | ms | `session.startup`, `chat.first_token`, `mcp.lazy_load`, `gateway.boot` |
| `_TURN_BUCKETS_MS` | 1s – 1h | ms | `turn.duration` |
| `_WATCHDOG_IDLE_BUCKETS_MS` | 1s – 4h | ms | `watchdog.idle` |
| `_TOOL_CALL_BUCKETS_MS` | 0.5ms – 1h | ms | `tool.call.duration` |
| `_SESSION_BUCKETS_MS` | 1s – 7d | ms | `session.duration` |
| `_CREDIT_BUCKETS` | 0.025 – 2500 | credit | `turn.credits` |
| `_USD_BUCKETS` | 0.001 – 100 | usd | `turn.cost_usd` |

`_TOOL_CALL_BUCKETS_MS` spans a sub-millisecond builtin read to a multi-minute
build in one instrument, so it keeps both ends: a `_FAST` ceiling would floor
every long shell command into the overflow bucket, and a `_TURN` floor would
collapse the entire builtin population onto boundary one. `_SESSION_BUCKETS_MS`
is the only family measured in hours and days — a speculative session removed
before its first turn lives seconds, while a dashboard tab left open across a
working week is ordinary — so it is densest from a minute to a few hours where
interactive sessions land, keeps sub-minute bounds because the short-lived
teardown paths (`unclaimed`, `destroyed`) are a real population, and carries a
7-day ceiling so a long-lived tab is not floored into overflow.

**Why the unit map is separate.** The dashboard's generic histogram aggregation
reports every statistic under `*_ms` keys and the frontend formats those with a
millisecond suffix, so `_HISTOGRAM_BUCKETS_MS` membership is a claim that the
instrument really is milliseconds. A credit or a dollar amount registered there
would make both the claim and the rendered value wrong. The two non-ms
instruments are instead claimed BY NAME by `_aggregate` ahead of the generic
branch and reported inside the `turn` block via `_amount_stats`, under
unit-neutral keys (`p50`, `p90`, `total`, …) each carrying its own `unit`.
`_amount_stats` also rounds to six decimals rather than one, because a sub-cent
per-turn cost rounds to `0.0` at the duration family's precision.

Each amount also carries `by_model`: the same total split by the emitted `model`
attribute, as `{model, count, total, mean}` rows sorted by total descending. Without
it the attribute would be recorded by the emitters and then discarded at the one
place that reports the amount, leaving "which model spent this" unanswerable from
the API. Percentiles are deliberately NOT repeated per model — that would add a
bucket array per row to answer a distribution question nobody asks of a single
model — and the split is **never truncated**, the same rule (and reason) as the
`cost_breakdown.by_model` block below. A turn whose `model` was empty is counted
under `unknown` rather than dropped, so the rows always sum to the pooled `total`
beside them.

**How the completeness guard finds instruments.** An instrument missing from both
maps silently inherits OTEL's default 0–10000 boundaries.
`test/metrics/test_provider_bucket_views.py` used to detect histograms by a
`.duration` name suffix, which is a convention rather than a property: it could
not see `kirocrew.embed.queue_wait` / `kirocrew.embed.inference` (millisecond
histograms without the suffix, which had been running on the default boundaries)
and by construction could never see a non-duration one. The guard now walks the
source by AST for `histogram(...)` calls, reading each instrument's name and its
`unit=` keyword, and asserts: every emitted histogram is registered in exactly
one map, every registered entry has an emit site, the ms map holds only
`unit="ms"` instruments, the unit map holds none, and no single name is emitted
with two different units.

**Exponential histograms are not usable here yet.** They would remove the
hand-tuned arrays, but the local JSONL shard is serialized with the SDK's
`to_json()`, and an exponential data point carries `scale`/`zero_count`/
`positive` instead of `bucket_counts`/`explicit_bounds`. The dashboard
aggregator classifies a point as a histogram by the presence of `bucket_counts`
and reads percentiles from `explicit_bounds`, so an exponential instrument would
be dropped from the payload entirely — a silent absence, which is worse than a
coarse bucket. Adopting them requires teaching the reader that shape first.

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
without a handler change. Scalar (non-histogram) metrics in `other` are
classified by the SDK's own JSON markers — a Sum's `data` block carries
`aggregation_temporality`/`is_monotonic`, a Gauge's carries neither. DELTA sums
keep summing across cycles/PIDs; CUMULATIVE sums and the lifetime-total gauges
named in `process_gauges.LIFETIME_TOTAL_METRICS` (process CPU/GC, whose newest
sample is a running total rather than a state) buffer
samples per (PID, process-identity, attrs) stream and reduce them time-ordered
after the scan (shard iteration order is not chronological). Routing both shapes
through one structure is also what keeps a lifetime total continuous across the
shards written while those four were still observable counters. The identity half
of the key is the resource-level `kirocrew.process.start_time` token the
exporter stamps: a changed token for the same PID is a deterministic process
boundary, so a reused PID starts a fresh stream even when the new process's
first snapshot already exceeds the old maximum — the one shape value-based
detection cannot see — while an unchanged token across provider rebuilds
stitches the rebuild segments into one stream. Within an identity-keyed stream
a value below the running maximum is treated as shard garbage (one identity is
one OS process, whose counters are monotonic), never banked as a reset. Only a
STRING token counts as an identity: a corrupt shard carrying any other type
there reads as identity-less rather than minting a stream that mutes the
heuristic. Identity-less streams (legacy shards, or platforms without a
start-time read) reduce under the counter-RESET value heuristic — a snapshot
below the stream's own maximum marks a process boundary and banks the finished
segment, while re-emitted snapshots at/above the maximum are no-ops — so
provider rebuilds stay idempotent and pre-change shards keep their exact
totals. Either way the stream's first in-window sample is subtracted as a
baseline so a process older than the window reports only in-window activity,
never its lifetime total (stream total = banked + live segment - baseline; add
across streams). Non-finite
scalars (json's Infinity/NaN literals) are rejected per point via one shared
coercion helper; gauges emit `kind: "gauge"` with `latest` (the newest sample,
never a sum). Gauge samples are keyed per exporting shard PID so
concurrent processes (gateway + MCP daemons) never collapse into one series: a
single-PID window keeps the plain shape, a multi-PID window reports the newest
process's reading as `latest` and a `pid=`-keyed `by_attr` breakdown. Malformed
shard records degrade per-point (a garbage value skips that point, a garbage
timestamp sorts oldest) rather than failing the endpoint. Percentiles are interpolated from bucket counts (made
meaningful by the DELTA temporality + explicit-bucket View). Security: the
user-configurable `telemetry.local_dir` and each shard pass `validate_file_path`
(sensitive-path check) before any read. Cross-process: metrics are emitted by
the ACP/gateway processes, so reading the durable shards is the only correct
path (an in-memory reservoir in the dashboard process would never see them).

**`GET /api/telemetry/collection`** (`api_collection_status`) is the small
companion route behind the recording switch. It reports the effective `enabled`
state, the metrics directory (`metrics_dir`), whether an env var (`env_pinned` /
`env_var`) or a `config.local.json` overlay (`overlay_override`) pins the setting,
and whether an OTLP endpoint is configured (`otlp_configured`). It is deliberately
separate from `/api/telemetry/startup`, which parses every metric shard in the
window to aggregate percentiles — far too much work for a panel that only needs to
know whether a switch is on. `otlp_configured` reports only THAT an endpoint
exists: the endpoint string never leaves `_telemetry_cfg()`, because it can carry
credentials in userinfo or query parameters. It is what lets the panel disable the
enable direction rather than offering a write that comes back 409, while leaving
disable available — which is where an opt-out matters most. Nothing on this route
is an egress control, so unlike the beacon there is no governance ceiling to
report.

**Both routes report the EFFECTIVE state, not the stored config flag.**
`_telemetry_cfg()` resolves consent the way the collector does — `env_pin()` from
`metrics/provider.py` overrides `telemetry.enabled` — so `enabled` on
`/api/telemetry/startup` and `/api/telemetry/collection` cannot read "off" while
metrics are being written, or "on" while nothing is. The pin comes from the
provider rather than a second read of the env var here, because two resolutions are
two things to keep in sync and a control that disagrees with the collector about
what "on" means is worse than no control. `env_pinned` is what lets the panel's
switch disable itself instead of offering a write the collector ignores.

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
| `surface` | str | **(#647, #1551)** canonical dispatch/session source. Dashboard-backed turns derive the source from the effective session key through `telemetry_channel_of` (so linked Slack/Telegram sessions retain their transport); Task Runner writes `taskrunner`; other writers use their dispatch origin (`cron`, `subagent`, `monitor`, `heartbeat`, `webhook`, `workflow`, …). `""` if unset |
| `agent` | str | **(#647)** agent id resolved for the turn; `""` if unset |
| `context_used` | int | **(#647)** context-window tokens occupied after the turn (int-coerced) |
| `context_window` | int | **(#647)** served context-window size in tokens (int-coerced) |
| `ctx_blocks` | dict[str,int] | per-turn injection breakdown: context block label → **characters** (never tokens); non-positive / non-numeric sizes dropped; `{}` when the turn injected nothing |
| `phase` | str | `session_start` (the first turn's one-off injection) vs `per_turn` (every later turn); `""` if unset |
| `stop_reason` | str | the turn's terminal stop reason read off the EVENT_COMPLETE event (`""` when the producer has none, e.g. a bare `TurnUsage` from `provider_last_turn_usage`). Free-form is fine HERE (the row store has no cardinality limit, unlike OTel attrs) — this is where per-agent stall analysis happens: joining `stop_reason` (`error: tool stall` / `stale_recover`) against the row's `agent` field attributes watchdog outcomes to free-form agent names retroactively |

The `surface` / `agent` / `context_used` / `context_window` fields (all #647),
the later `ctx_blocks` / `phase` pair, and `stop_reason` are all **additive** —
every field defaults (`""` / `{}` / `0`) so existing callers stay valid and
shards predating a field (which lack its key) remain parseable; readers must
tolerate their absence.
`context_used` / `context_window`
are read from the provider at the persist call site via
`usage.read_context_tokens(source)`, which calls the provider's public
`context_used_tokens()` / `context_window_tokens()` accessors
(`providers/base.py`, implemented for ACP in `providers/acp.py` +
`acp/session_provider.py`) behind `getattr` guards and returns `(0, 0)` on any
missing accessor or exception — so non-ACP providers and test doubles record
zeros and the analytics helper never breaks the turn it measures. `surface`
lets channel-backed turns and background dispatch surfaces (cron/subagent/
monitor/heartbeat/webhook/taskrunner/workflow) retain their canonical source;
`context_occupancy` normalizes the historical `task_runner` spelling to
`taskrunner` when rows are read. Zero-token surfaces (cron `script=`/`command=`
modes, heartbeat maintenance ticks) never call a model and must not write a row.

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
must stay complete.
A block owns the span from its opener up to **the earlier of** the next opener and
its OWN closer (`_CLOSERS`, keyed by the same labels — only the closers the
assembly actually emits are listed, so extending it is a data change rather than a
scanner edit). **A label with an opener in `_MARKERS` but no `_CLOSERS` entry keeps
the absorbing behaviour this table exists to remove**, so the two are kept in step
by grepping the assembly for `[End ` / `[END ` rather than by adding a closer only
when one is noticed. One opener can have MORE than one closer spelling — a hook
context is closed `[End of hook context]` by `context.py` and `[End hook context]`
by `chat_runner.py` — and an entry covering one spelling silently leaves the other
block absorbing what follows it, so such a label carries an alternation rather than
whichever spelling was found first. The closer taken is the LAST match before the next opener, not the
first: a block's content can quote its own closer — a custom agent prompt that
documents the envelope it is injected into embeds `[END AGENT SYSTEM PROMPT]`
verbatim — and first-match would end the block at that quotation and book the rest
of its real body as `unclassified`. Last-match is right by construction, because
nothing of the block follows its real closer, so any earlier occurrence in range is
content. The closer search is bounded by the next opener, which is what
keeps a WRAPPER correct: `[SESSION CONTEXT` closes long after the memory family
opens inside it, never finds its own closer in range, and ends where it always did
— unbounded, it would swallow every block nested within. The blank line a block
emits right after its closer stays with that block, so a closed block does not
leave a two-character crumb behind it. Escaping matters in one place worth naming:
`[End of skill]` is a PREFIX of `[End of skills]`, so an unanchored closer would
let one loaded skill claim the whole skills index that follows it.
Characters that fall between a block's closer and the next opener therefore
surface as `unclassified`, as do leading bytes before the first marker. This is
the honest reading and it replaces a silent one: without closer awareness a span
ran all the way to the next opener, which turned *unattributed* bytes into
*MIS-attributed* ones with no way for a reader to tell a genuinely large block from
a small one that had absorbed its neighbours. The trigger is ordinary rather than
exotic — the blocks between two openers are conditional (workspace identity and the
docs pointer are skipped for a custom agent, the memory family for a session sealed
from the user's memory), so their absence is exactly what lets an earlier block
absorb everything downstream of it. Measured on one real session, a ~470-character
`[USER PROFILE]` block was reported as 8,116.
The returned sizes sum EXACTLY to `len(prompt)` (closure); the user's
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
the trace starts where the recording does. Each turn also carries the row's
`credits` and `duration_ms` when the shard recorded usable numbers (same
drop-the-field-not-the-row rule as `TURN_USAGE_FIELDS`): injection and billing
live on the same shard row, so the trace returns both in one walk rather than
making the panel re-join through the usage-turns reader what was never apart.
The chat Activity panel renders them as a per-turn credits column that appears
only when at least one traced turn carries billing — pre-recorder history stays
three columns instead of growing an all-dash one.
`handlers/telemetry.py::api_context_trace`
serves it as `GET /api/telemetry/context-trace?slot=<session key>` (`400` when
`slot` is missing or blank), independent of the `telemetry.enabled` switch since
these rows are always written. The endpoint is **dashboard-only**: unlike
`/api/usage/turns` this reader has no row-ownership model, and its rows carry
the turn's billing, so an app caller is refused outright with the standard
indistinguishable `404` (`code: not_found`) and the refusal is SEL-audited
(deny-by-default, App Kit §5.2) — an app that needs its own turns' billing has
`/api/usage/turns`.

**Row-timestamp parsing has one owner.** `usage._parse_row_dt` is the single
spelling for reading a stored row timestamp (`Z` rewritten to `+00:00` for
py3.10's `fromisoformat`; a naive stamp left naive so a caller's
`.timestamp()` reads it in local time); `_parse_row_ts` derives the epoch form,
and every shard/row reader in the module (`slot_spend`, `context_occupancy`,
`cost_breakdown`, `slot_turn_usage`, the token-history and transcript-day
readers) resolves timestamps through them. Two readers of the same rows must
not disagree about which rows a window contains. `_usage_number` is likewise
the one guard for copying a numeric field out of a row (bool is not a count;
ints are accepted directly because `math.isfinite` would overflow on an
oversized int; a non-finite float is dropped).

**Per-turn usage rows for one session.** `usage.slot_turn_usage(slot, days)` is
the per-turn drill-down under `slot_spend`'s aggregate: one row per turn with
`ts`, `model`, and the numeric fields named by `usage.TURN_USAGE_FIELDS`
(tokens in/out, cache create/read, `credits`, `cost`, `duration_ms`, and the
context meter pair). A non-numeric or non-finite field is dropped from its row,
never the row itself. `handlers/telemetry.py::api_usage_turns` serves it as
`GET /api/usage/turns?slot=<session key>[&days=N]` (`400` on a missing slot;
`days` clamps to `[1, SPEND_WINDOW_DAYS]` rather than refusing, because shards
beyond the window are retired anyway). This is the endpoint an **app** is
granted through its manifest's `permissions.api` to account for what its own
agent slots cost — apps otherwise have no path to credits, and the shard files'
location and row shape stay this module's private contract. **App isolation is
ROW-level** (App Kit §5.2, deny-by-default): each row is stamped with the
owning app at write time (`_build_token_record`'s `app` field, threaded from
the turn's slot), and an app caller receives only rows stamped with its own
app — however the slot is named, and whether or not it is still live. A
live-slot ownership check was deliberately rejected: it leaks on slot-name
reuse (a recreated slot vouches for the previous owner's retained rows) and
denies an app its own completed sessions, which are exactly what an audit
reads. A foreign slot key answers `200` with no rows — indistinguishable from
a slot that never ran — and rows predating the stamp are invisible to app
callers. A **disabled app is refused outright** (`is_app_enabled`,
deny-by-default, the same gate the opt-in builtin routes wrap every handler
in): disable revokes read access, not only future writes. Every app-caller
decision is SEL-logged — including a malformed request's refusal — and SEL
plus the enablement probe run off-loop. The window is enforced **per row**,
not only per shard file (the oldest shard in a window covers a whole day);
a row whose timestamp cannot be parsed is excluded — accounting excludes
what it cannot date. Dashboard users (empty request app) read any slot; the
Telemetry panel's Spend table is the dashboard consumer — each session row
expands into its per-turn rows through this endpoint, which is where a
mid-session model switch or a single runaway turn becomes visible (an average
hides both).
**Stamping boundary:** rows are stamped at the two write sites that can run
app-owned work — the dashboard chat runner (the slot's `_app`) and the
subagent completion path (`info.app`, an app-dispatched subagent's spend).
The task-runner, workflow, Slack and background-one-liner writers do not
stamp because those surfaces are not app-owned — an empty stamp there is the
correct value, not a gap. Webhook-session rows are currently unstamped and
therefore invisible to app callers; if webhook sessions gain app ownership,
that write site must stamp too. Same independence from the
`telemetry.enabled` switch as the context trace.

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

**Channel comes from the slot key, not from `surface`.** New writes now derive
`surface` from the effective session identity, but historical rows may contain a
wrong, non-empty value (for example a Telegram turn stamped `dashboard`). The
row format has no schema version or writer/trust marker that distinguishes those
legacy values from corrected writes. Cost/category attribution therefore keeps
the slot key authoritative; preferring a non-empty `surface` would silently
misattribute existing history. The stored slot/session key remains stable across
the write-side correction and is still the compatibility boundary for this
reader.

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
(aggregation, skips, latest-turn wins, historical Task Runner spelling),
`test/metrics/test_cost_breakdown.py`
(channel attribution incl. the bare dashboard slot form, no-truncation,
prior-window deltas, band bucketing, post-compaction slope, growth
withholding, non-finite rejection), `test/test_dashboard_chat.py` (effective
session source at the dashboard write site),
`test/test_turn_duration_task_runner.py` (canonical Task Runner writes), and
`test/metrics/test_telemetry_titles.py`
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
  configures an OTLP endpoint. It also carries the **recording switch** over
  `telemetry.enabled` — a second, independent control from the beacon toggle above
  — fed by `GET /api/telemetry/collection` and disabled when an env var, a
  `config.local.json` overlay, or a configured OTLP endpoint means the write
  cannot take effect (or would start egress).
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
