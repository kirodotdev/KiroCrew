# Exporting Kiro Crew metrics over OTLP

Kiro Crew records its metrics through the OpenTelemetry SDK. By default they go
nowhere but this machine: a local JSONL sink under `~/.kiro/crew/metrics`, which
the dashboard's Telemetry panel reads. This page is about the other option —
pushing the same metrics to an **OpenTelemetry Collector**, and from there to
whatever backend you already run (Amazon CloudWatch, Datadog, or any service that
accepts OTLP).

Nothing here is on by default, and turning it on takes two separate decisions:
collection, then egress.

## Default posture

| | Default | What it means |
|---|---|---|
| `telemetry.enabled` | `false` | Nothing is recorded. Metric call sites are cheap no-ops. |
| `telemetry.otlp_endpoint` | `""` (empty) | Nothing leaves the machine. Local JSONL sink only. |
| `telemetry.kiro_export` | `false` | Nothing is sent to the Kiro telemetry endpoint. See [Sending product metrics to Kiro](#sending-product-metrics-to-kiro). |
| OTLP exporter package | not installed | The OTLP exporter is not importable until installed separately. |

Collection and egress are deliberately separate switches. Enabling collection
gives you the local sink and the Telemetry panel; it does **not** send anything
anywhere. Egress needs the endpoint set *and* the exporter package installed.

The local sink is never replaced by OTLP — it is additive. When you configure an
endpoint you get both readers, so the dashboard keeps working and you keep a
local copy.

## Turning it on

### 1. Install the exporter

From a source checkout, install the declared extra without replacing the editable
Kiro Crew install:

```bash
pip install -e ".[otlp]"
```

For an existing install, install the extra's pinned distribution directly:

```bash
pip install "opentelemetry-exporter-otlp-proto-http==1.44.0"
```

The project is not published on the default package index, so
`pip install "kirocrew[otlp]"` is not a supported command. The exporter uses
**OTLP over HTTP only** — there is no gRPC exporter, so your collector needs its
OTLP receiver's `http` protocol enabled (port 4318 by convention, not 4317).

If the exporter package is missing but an endpoint is configured, Kiro Crew logs
a warning and stays local-only rather than failing to start.

### 2. Enable collection

Either edit `~/.kiro/crew/config.json`:

```json
{
  "telemetry": {
    "enabled": true,
    "otlp_endpoint": "http://localhost:4318/v1/metrics"
  }
}
```

or set the environment variable, which overrides the config flag in both
directions and is the convenient form for containers and one-off debugging:

```bash
KIROCREW_TELEMETRY=1 kirocrew gateway
```

**`otlp_endpoint` is the full signal URL, including `/v1/metrics`.** It is passed
to the exporter as-is; no path is appended for you. `http://localhost:4318` alone
will not work.

### 3. Confirm egress started

On startup the gateway logs the local sink and, separately, the fact that metrics
are leaving the machine:

```
telemetry enabled; local JSONL sink at /home/you/.kiro/crew/metrics (otlp=on)
telemetry OTLP export active; metrics leave this machine (telemetry.otlp_endpoint)
```

The second line is the one to look for. The endpoint value is never logged — only
the destination name — so a credential embedded in the URL does not end up in the
journal.

### Other settings worth knowing

| Key | Default | Notes |
|---|---|---|
| `telemetry.export_interval_seconds` | `60` | Applies to both readers. |
| `telemetry.local_dir` | `~/.kiro/crew/metrics` | Local JSONL shard directory. |
| `telemetry.retention_days` | `0` | `0` never prunes by age. |
| `telemetry.max_total_mb` | `0` | `0` never prunes by size. |

## Temporality: the setting that decides whether your backend accepts the data

Counters can be exported two ways. **Cumulative** sends the running total every
interval; **delta** sends only what changed since the last export. Backends
disagree about which they want, and sending the wrong one is the most common
reason metrics arrive but look wrong — a cumulative counter graphed as a rate, or
a delta counter treated as a total that appears to reset constantly.

Kiro Crew exports **delta** by default, for counters, up-down counters and
histograms alike. That is the same encoding its local JSONL sink uses, so both
destinations describe the same instrument the same way; it is also the cheaper
one, because a cumulative histogram re-sends every bucket of every series on
every interval whether or not anything happened, while an idle delta series sends
nothing at all.

To match a backend that wants something else, set the standard OpenTelemetry
variable. When it is set, Kiro Crew passes no preference of its own and the
setting is in full control:

```bash
# Cumulative — the OpenTelemetry default. CloudWatch, Prometheus-style backends.
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=CUMULATIVE

# Delta — what Datadog and most product-analytics ingests expect. Equivalent
# to the unset default for every instrument Kiro Crew currently emits (the
# unset default also maps up-down counters to delta; this value keeps them
# cumulative, and Kiro Crew emits none).
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=DELTA

# Delta for counters and histograms, cumulative for up-down counters.
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=LOWMEMORY
```

Set it in the gateway's environment, not the collector's — it governs how Kiro
Crew *encodes* what it sends.

Three things are unaffected by this setting, and knowing that saves debugging time:

- **Gauges have no temporality at all.** Every instrument reporting a
  point-in-time reading (thread counts, memory, CPU and GC totals, install
  inventory, the probe-failure count) is a gauge, so the preference does not
  apply to them. Nothing Kiro Crew exports is a cumulative sum, which is what
  lets a consumer aggregate one datapoint at a time instead of holding
  whole-hour state per host.
- **The local JSONL sink always uses delta** for counters and histograms,
  whatever this variable says. It is a separate reader with its own encoding, and
  the dashboard aggregator depends on it.
- **The Kiro destination always uses the OpenTelemetry delta preset** (delta for
  counters, observable counters and histograms; cumulative for up-down counters),
  whatever this variable says. That is the encoding the Kiro endpoint receives
  from every other Kiro client, so the variable stays a setting for your own
  collector only. See
  [Sending product metrics to Kiro](#sending-product-metrics-to-kiro).

### If you already export these metrics

These five instruments are gauges that hold lifetime totals, under these
names:

- `kirocrew.process.cpu.seconds`
- `kirocrew.process.gc.collections`
- `kirocrew.process.gc.collected`
- `kirocrew.process.gc.uncollectable`
- `kirocrew.inventory.probe.failures`

Each reading is the total since the exporting process started, carried as a
gauge rather than a monotonic sum. Do not apply a counter function (`rate()`,
`increase()`, delta-from-cumulative) to them: take the difference between
consecutive samples instead.

Handle a restart the way you would for any gauge you difference: **clamp negative
increments to zero**. `service.instance.id` identifies the INSTALL, not the
process, so a restart does not automatically start a new series at your backend —
and if the new process happens to reuse the old PID (the kernel wraps through
`pid_max`), the resource labels are identical while the totals begin again near
zero, so one consecutive-sample difference across that boundary comes out
negative. That is one datapoint at a restart boundary, not a corrupted series, and
a clamp is the standard treatment.

Kiro Crew's own dashboard is not exposed to this: the local JSONL exporter stamps
each record with the writing process's OS start-time token, and the aggregator
keys each stream by (PID, that token), so a reused PID lands in a brand-new
stream. The token is deliberately host-local and reboot-unique, so it never leaves
the machine — which is why an OTLP consumer has to do the clamp instead.

You can also convert temporality inside the collector with the
`cumulativetodelta` processor, which is the better lever when one collector feeds
two backends that disagree.

## Collector configuration

The examples below are collector configs. Point Kiro Crew at the collector's OTLP
HTTP receiver and let the collector fan out.

**Distribution matters.** The `otelcol` core distribution ships the OTLP receiver
and a small set of exporters. The vendor exporters below (`awsemf`, `datadog`)
are only in **`otelcol-contrib`**. Using core and wondering why an exporter is
"unknown type" is the usual first mistake.

**These samples are illustrative, not tracked.** Vendor exporter options change
with collector and vendor releases that this repository does not follow, so treat
each vendor's own collector documentation as authoritative and these blocks as a
starting shape. What is stable — and what this page actually owns — is the Kiro
Crew side: the two consent switches, the full-signal-URL requirement, and the
temporality preference.

### Shared receiver

Every example starts here:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318
```

Bind to `127.0.0.1` when the collector runs on the same host as the gateway. A
`0.0.0.0` bind accepts metrics from anywhere on the network and needs its own
authentication story.

### Amazon CloudWatch

The `awsemf` exporter writes CloudWatch Embedded Metric Format, which turns each
metric into a CloudWatch metric via a log group.

```yaml
exporters:
  awsemf:
    region: us-west-2
    namespace: KiroCrew
    log_group_name: /kirocrew/metrics

service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [awsemf]
```

Use `CUMULATIVE` temporality with this path. Credentials come from the
collector's own environment via the standard AWS chain, so the collector needs
`logs:PutLogEvents` and `logs:CreateLogStream` on that log group.

### Datadog

```yaml
exporters:
  datadog:
    api:
      key: ${env:DD_API_KEY}
      site: datadoghq.com

processors:
  # Datadog expects delta counters. Convert here if you would rather not set the
  # temporality preference on every gateway.
  cumulativetodelta: {}

service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [cumulativetodelta]
      exporters: [datadog]
```

Set `DD_API_KEY` in the collector's environment rather than inlining it.

### Any other OTLP-compatible backend

Most analytics and observability services accept OTLP/HTTP directly. Forward with
`otlphttp` and add whatever authentication header that service documents:

```yaml
exporters:
  otlphttp/vendor:
    endpoint: https://otlp.example-vendor.com
    headers:
      authorization: ${env:VENDOR_API_KEY}

service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [otlphttp/vendor]
```

Check that vendor's documentation for three things, because they vary: the exact
endpoint (some want the base URL and append `/v1/metrics` themselves, others want
the full signal path), the header name, and the temporality they expect.

One pipeline can also carry several exporters, which is the reason to run a
collector at all rather than pointing the gateway straight at a vendor.

## What gets exported

The OTLP reader shares one meter provider with the local JSONL sink, so every
`kirocrew.*` instrument the process records is exported — counters, histograms
and gauges alike. The full roster lives under "Instrumented signals" in
[`../system-specs/modules/metrics.md`](../system-specs/modules/metrics.md). The
gauge families are these two:

- **`kirocrew.process.*`** — this process's own resource behavior: Python and OS
  thread counts, open file descriptors, current and peak RSS, cumulative CPU
  seconds, and per-generation GC counters. This family also carries histograms
  (sampled RSS and CPU utilization).
- **`kirocrew.inventory.*`** — what this install has configured: active cron jobs,
  armed monitor loops, installed skills, whether memory has been migrated,
  knowledge-source and lesson counts, MCP server counts by class, and a
  fixed set of feature switches. Plus `kirocrew.inventory.probe.failures`, which is
  absent on a healthy host and appears only when a probe cannot read its source —
  that is how you tell a broken probe from a host that stopped exporting, since
  both otherwise look like a missing series.

One threshold is worth knowing when you set alerts on the counts, because it is
the product's own limit rather than a round number: `200` is the point the lesson
store starts pruning. The gauges publish the raw number and leave the banding to
your queries, so you can move that line without a Kiro Crew change.

Resource attributes identify the sending process and are attached to every
series, so they are what you group by. `service.name` is always `kirocrew`.

### A destination can receive less than everything

The paragraph above describes the destination you configure: `otlp_endpoint`
sends your own collector every instrument the process records, under the full
resource, with no attribute Kiro Crew added on its way out. That is unchanged and
is what this guide is about.

A destination is also allowed to receive only PART of that. `OtlpDestination`
carries three optional fields, and a build that assembles its own destination --
rather than the one you configure through `otlp_endpoint` -- can set them:

- `allow` -- only metrics whose name is in this set are sent. Unset means
  everything, which is what your endpoint gets.
- `point_attributes` -- fixed key/value pairs added to every data point of that
  destination. The one reviewed key today is `ClientType`.
- `resource_allow` -- that destination's batches carry a resource reduced to these
  keys, so a destination that may see only `service.name` and `service.version`
  does not receive `service.instance.id`.

Three things to take from this if you operate a collector:

- **Your destination is not affected by another one.** Each destination's
  narrowing is applied to its own exporter, so nothing another destination
  declares can add an attribute to, or remove a metric from, what reaches yours.
- **The local JSONL sink is never narrowed.** It records everything the process
  collected, under the full resource, which is what makes it the place to check
  what was actually measured on a machine.
- **A narrowing can only reduce.** None of the three fields can add a metric, relax
  the consent gate, or bypass the attribute sanitiser; a destination that sets
  none of them behaves exactly as this guide describes.

### Before you build a per-host dashboard

Kiro Crew sets `service.instance.id` explicitly to a random, persisted install
identifier, so group by that attribute to distinguish machines and follow an
install across gateway restarts. It is not derived from a hostname or username.
`process.pid` is a separate resource attribute: it prevents the gateway, agents,
and app processes on one install from interleaving their per-process gauges.

That process attribute means an individual series normally turns over when a
restart receives a different PID even though the install identity stays stable.
For longitudinal host panels, aggregate the relevant process series by
`service.instance.id`; for lifetime-total gauges, also treat a new PID as a reset.
If the data home is unwritable and the install-id mint fails, Kiro Crew omits its
explicit id and the SDK may fall back to a per-process identity, so cross-restart
continuity is unavailable on that broken host.

### What these can and cannot tell you

They describe **the machines you run and have opted in**, and nothing wider. Both
switches default off and OTLP additionally needs the optional exporter package,
so an aggregate over these gauges is a statement about your own fleet — useful
for spotting a host that stopped scheduling crons or whose skills tree drifted,
not a measurement of how a feature is used in general. Kiro Crew's own install
analytics are a separate, deliberately unrelated channel (`beacon.py`), for
reasons its docstring sets out.

One practical consequence: `kirocrew.inventory.*` comes from the **gateway process
only**, while `kirocrew.process.*` comes from every telemetry-enabled process
(gateway, MCP gateway daemon, spawned agents). Install-level facts are identical
across those processes, so publishing them from each one would count a single host
once per process. If you see process metrics from a host but no inventory metrics,
the gateway on that host is not running or not exporting — that is the signal, not
a gap in collection.

## Sending product metrics to Kiro

Everything above is about a collector *you* run. This section is about the one
destination Kiro Crew ships with: the Kiro telemetry endpoint, which is how the
people who build Kiro Crew see whether a feature is used and whether a release
helped. It is a second, separate switch, it is off by default, and it sends a
much smaller set of metrics than your own collector gets.

| | Value |
|---|---|
| Switch | `telemetry.kiro_export` (default `false`), and `telemetry.enabled` must also be on |
| Endpoint | `https://telemetry.<region>.kiro.dev/v1/metrics`, standard OTLP/HTTP over TLS |
| Region | `telemetry.kiro_export_region`: `us-east-1` (default) or `eu-central-1` |
| What is sent | Only the metrics in the table below, each with `ClientType=kiro-crew` |
| Who you are | Your existing Kiro sign-in, when there is one |
| Cadence | Once per `telemetry.export_interval_seconds` (60s), on the reader thread |
| Temporality | Delta for counters, observable counters and histograms; cumulative for up-down counters. Fixed: `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` does not change it |

The endpoint is a constant in the code. No setting can turn it into a plain HTTP
URL or aim it at another host; the region chooses between the two spellings above
and nothing else. A region that is neither of them switches the destination off
rather than guessing which one was meant.

### Turning it off

Any one of these is enough. The first two take effect on the next export, with
no restart:

- `kirocrew config set telemetry.kiro_export false`
- the recording switch in Settings -> Privacy, which turns off `telemetry.enabled`
  and with it every destination

The rest are read from the gateway's own environment, so they take effect when
the gateway (and the processes it starts after that) next starts with them set.
Exporting one in another shell does not reach a gateway that is already running:

- `DO_NOT_TRACK=1` in the environment (any value other than `0`/`false`/`no`/`off`
  counts, per the cross-product convention)
- `KIROCREW_TELEMETRY_KIRO_EXPORT=0` in the environment
- running the gateway with `--test-mode`, which switches it off unconditionally

An operator's own `telemetry.otlp_endpoint` is unaffected by all of these, and
the local JSONL sink still records everything either way, so you can always read
on your own machine what was measured.

### The exact metrics that leave the machine

Nineteen names, and nothing else. Every other instrument stays in the local sink
and in your own collector: most of them exist for local debugging. A histogram on
this list is sent as a native OTLP histogram, with its explicit bucket bounds and
delta counts, the way other Kiro clients send distributions;
`kirocrew.session.duration` is one. `kirocrew.turn.duration` is not on the list:
it carries the configured model id and provider, which are not a closed set.

| Metric | Type | Attributes |
|---|---|---|
| `kirocrew.session.started` | counter | `session_source`, `harness` |
| `kirocrew.session.duration` | histogram | `end_reason`, `session_source` |
| `kirocrew.session.idle_expired` | counter | `turn_active`, `orphaned` |
| `kirocrew.subagent.spawned` | counter | `concurrency`, `batched` |
| `kirocrew.context.compactions` | counter | `success` |
| `kirocrew.cron.fires` | counter | `kind`, `trigger` |
| `kirocrew.artifact.created` | counter | `kind`, `source`, `kind_auto` |
| `kirocrew.workflow.runs` | counter | `authored`, `replay` |
| `kirocrew.approval.decisions` | counter | `decision`, `security_deny` |
| `kirocrew.inventory.crons.active` | gauge | none |
| `kirocrew.inventory.monitor_loops.active` | gauge | none |
| `kirocrew.inventory.skills.installed` | gauge | none |
| `kirocrew.inventory.memory.migrated` | gauge | none |
| `kirocrew.inventory.knowledge.documents` | gauge | none |
| `kirocrew.inventory.lessons` | gauge | none |
| `kirocrew.inventory.mcp.servers` | gauge | `class` |
| `kirocrew.inventory.members` | gauge | none |
| `kirocrew.inventory.config.toggle` | gauge | `key` |
| `kirocrew.inventory.probe.failures` | gauge | `probe` |

Every attribute value above comes from a closed set fixed in the code. None of
them is a name, a path, a URL, an id, a model identifier or any text you typed.
`harness` names which agent harness ran the session (for example `kiro` or
`claude_code`), from the closed vocabulary in `metrics/harness.py`.
A name joins this table only in a reviewed change that also changes
`PRODUCT_METRICS` in the code, and a test fails if the two ever disagree.

Four resource attributes travel with the data, narrowed down from the fuller set
your own collector receives: `service.name`, `service.version`, `os.type` and
`process.pid`. The install id (`service.instance.id`) and the `host.*` readings
are **not** sent to this destination: they identify a machine, and this
destination counts none.

### How you are identified

If you are signed in to Kiro, the request carries that sign-in: an
`Authorization: Bearer` header, plus `x-kiro-token-type` and
`x-kiro-profile-arn` where your sign-in kind needs them. It is the same token
Kiro Crew already holds for model calls, read from its cache. Exporting never
starts a sign-in and never refreshes a token: one that is missing, unreadable or
close enough to expiry that it would need renewing is treated as no sign-in at
all.

A headless host that authenticates with `KIRO_API_KEY` in the environment rather
than an interactive sign-in sends that key the same way, as
`Authorization: Bearer` with `x-kiro-token-type: API_KEY`. It is read only when
no stored sign-in can serve the request, which is the order the rest of the
product resolves the two in.

If you are not signed in, the request carries one header instead,
`x-kiro-machineid`, a one-way hash (SHA-256 under a fixed prefix) of the
installation id Kiro Crew already keeps on disk. The endpoint requires one of the
two headers, which is the only reason it exists; reading no installation id means
no request is sent. Nothing is counted per machine from it, it is never a metric
attribute, it never reaches the local sink, and the prefix means it cannot be
matched against the same installation id used anywhere else.

The token, the profile ARN and the machine id ride in headers only. None of the
three is ever logged, at any log level.

### What happens when the endpoint does not answer

The worst case is a lost minute of numbers. There is no disk queue, nothing is
held between intervals, and a long outage holds no growing data.

| Case | What happens |
|---|---|
| Slow endpoint | The request times out after 10 seconds, on the reader thread. Nothing you type waits on it. |
| Down or unreachable | The interval is dropped. The next one is a minute later. |
| Refused (4xx, including an expired token's 401) | The interval is dropped, and never retried as not-signed-in. Three refusals in a row pause this destination for an hour, so a bad configuration does not retry every minute. |
| Server error (5xx) | The interval is dropped. It does not count toward the pause: it is weather, not an answer. |
| Any failure at all | Contained at this destination. Your own collector and the local sink keep working. |
| Logging | One warning for the first failure, then debug only, and one info line when it starts working again. |

### Why `telemetry.enabled` may refuse to turn on from the dashboard

The Privacy panel's recording switch is offered as local-only, so the config
route refuses to turn it on (HTTP 409) on a host where doing so would start
egress. With `telemetry.kiro_export` set to `true` in the config file, that is
such a host, and the switch says so instead of quietly sending. Turn collection
on in the config file, which is where the destination was chosen.

### Traces are not exported

Kiro Crew emits the OpenTelemetry **metrics** signal only. There are no spans
and no log records, so a trace-oriented backend (Langfuse, Jaeger, Tempo, or a
vendor's trace view) receives nothing from it, and nothing in this page turns
traces on. Pointing `otlp_endpoint` at a `/v1/traces` or `/v1/logs` URL does not
change that: the collector rejects every metric batch. The gateway logs a
warning naming the destination when the endpoint's path is one of those routes.
Trace support is tracked in issue #1643.

### What is deliberately not exported

Reading these metrics should not tell you what a user is working on:

- **No MCP server names.** The MCP gauge publishes two counts, `first_party` and
  `third_party`. Server names are read to classify and then discarded, because a
  roster is user-chosen text — unbounded as a label and revealing as data.
- **No knowledge or lesson content.** Those two gauges publish a count and nothing
  else: no source titles, paths, or text, and no per-document attributes. The count
  is deliberately raw rather than banded — the destination is your own collector
  describing your own machines, and a band would hide exactly the growth the gauge
  is there to show.
- **No paths, endpoints, prompts, message content, or session identifiers**
  anywhere in a metric name or attribute value.
- **A subsystem that cannot answer produces no data point** rather than a zero.
  A missing series means "could not read"; a `0` means a real zero. If a series
  you expect is absent, that is the gauge telling you something.

## Verifying it end to end

### Automated

```bash
pytest test/metrics/test_otlp_wire_e2e.py
```

Two tiers. The first drives a real build through a real exporter and asserts the
instrument roster, resource-attribute fidelity, attribute values, and
temporality. The second stands up an in-process OTLP receiver on loopback,
exports to it with the real OTLP exporter, and decodes the protobuf — that tier
skips unless the OTLP exporter package is installed.

### Against a real collector

The automated tiers stop at the process boundary: they prove what Kiro Crew
serializes and puts on the wire, not what a collector does with it. Closing that
last gap needs a collector binary and outbound network, so it is a manual step —
and one worth doing once against whatever collector you actually run, rather than
against a stand-in.

Point a collector at Kiro Crew using the [Shared receiver](#shared-receiver)
config above, and give it an exporter you can read directly instead of a vendor
backend, so the payload is inspectable:

```yaml
exporters:
  # `debug` ships in every collector distribution; `file` needs a distribution
  # that bundles the contrib file exporter (the standard `otelcol` build does).
  debug:
    verbosity: detailed

service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [debug]
```

Then start the gateway with `otlp_endpoint` set to that collector, wait one export
interval, and read what arrived. Expect the `kirocrew.process.*` and
`kirocrew.inventory.*` gauges, any other `kirocrew.*` instruments the process has
recorded since start, and the resource attributes described above.

Some instruments are legitimately absent and their silence is not a failure: the
Linux-only thread and file-descriptor gauges on macOS, and the knowledge, MCP, and
monitor-loop gauges on an install that has never ingested a document, configured a
server, or armed a loop. `kirocrew.inventory.probe.failures` is the series that
distinguishes a probe that broke from a source that is simply not there.

If you fetch a collector binary rather than using one you already run, verify it
against the checksums published with that release; each release also publishes
cosign `.sig` and `.pem` files, which are the stronger check and worth using if you
are vendoring the binary into your own infrastructure.

### By hand, with no collector at all

The quickest smoke test is to watch the local sink, which is fed by the same
recorder:

```bash
KIROCREW_TELEMETRY=1 kirocrew gateway
# after one export interval (60s by default)
ls ~/.kiro/crew/metrics/
latest="$(ls -t ~/.kiro/crew/metrics/metrics-*.jsonl | head -1)"
tail -n 1 "$latest" | python3 -m json.tool | head -40
```

If instruments appear there but not at your backend, the problem is the endpoint,
the collector, or temporality — not collection.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No `telemetry OTLP export active` line | `otlp_endpoint` empty, or the OTLP exporter package is not installed — check for the warning naming the missing package. |
| `OTLP exporter init failed` warning | Malformed endpoint. The message deliberately omits the URL, since it can carry a credential. |
| Local shards fill, nothing at the backend | Endpoint missing `/v1/metrics`, collector on 4317 (gRPC) instead of 4318 (HTTP), or the collector's `http` protocol not enabled. |
| `points at an OTLP traces route` warning | The endpoint ends in `/v1/traces` (or `/v1/logs`). Kiro Crew exports metrics only, so a trace backend such as Langfuse receives nothing. See "Traces are not exported". |
| Metrics arrive but counters look like resets | Temporality mismatch. See the temporality section. |
| `unknown type: "awsemf"` at collector startup | Running the core `otelcol`; vendor exporters need `otelcol-contrib`. |
| One expected series is absent | That probe could not read its source. Absence is a gap, never a zero. |
| Nothing recorded at all | Collection consent: `telemetry.enabled` is `false` and `KIROCREW_TELEMETRY` is unset. |
