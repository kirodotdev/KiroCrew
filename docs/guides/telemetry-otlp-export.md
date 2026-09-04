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
| `kirocrew[otlp]` extra | not installed | The OTLP exporter is not even importable. |

Collection and egress are deliberately separate switches. Enabling collection
gives you the local sink and the Telemetry panel; it does **not** send anything
anywhere. Egress needs the endpoint set *and* the extra installed.

The local sink is never replaced by OTLP — it is additive. When you configure an
endpoint you get both readers, so the dashboard keeps working and you keep a
local copy.

## Turning it on

### 1. Install the exporter

```bash
pip install "kirocrew[otlp]"
```

This pulls `opentelemetry-exporter-otlp-proto-http`. Transport is **OTLP over
HTTP only** — there is no gRPC exporter, so your collector needs its OTLP
receiver's `http` protocol enabled (port 4318 by convention, not 4317).

If the extra is missing but an endpoint is configured, Kiro Crew logs a warning
and stays local-only rather than failing to start.

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
telemetry OTLP export active; metrics leave this machine (default)
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

# Delta — what Datadog and most product-analytics ingests expect. Also the
# default when the variable is unset, so setting it changes nothing.
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=DELTA

# Delta for counters, cumulative for up-down counters.
OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=LOWMEMORY
```

Set it in the gateway's environment, not the collector's — it governs how Kiro
Crew *encodes* what it sends.

Two things are unaffected by this setting, and knowing that saves debugging time:

- **Gauges have no temporality at all.** Every instrument reporting a
  point-in-time reading (thread counts, memory, CPU and GC totals, install
  inventory, the probe-failure count) is a gauge, so the preference does not
  apply to them. Nothing Kiro Crew exports is a cumulative sum, which is what
  lets a consumer aggregate one datapoint at a time instead of holding
  whole-hour state per host.
- **The local JSONL sink always uses delta** for counters and histograms,
  whatever this variable says. It is a separate reader with its own encoding, and
  the dashboard aggregator depends on it.

### If you already export these metrics

Five instruments used to be exported as monotonic cumulative sums and are now
gauges, under unchanged names:

- `kirocrew.process.cpu.seconds`
- `kirocrew.process.gc.collections`
- `kirocrew.process.gc.collected`
- `kirocrew.process.gc.uncollectable`
- `kirocrew.inventory.probe.failures`

The reading did not change — each is still the total since the exporting process
started — but the wire type did, so a backend that was applying a counter
function (`rate()`, `increase()`, delta-from-cumulative) to them will need
re-pointing: take the difference between consecutive samples instead. A backend
that rejects a type change on an existing series may also need the old series
dropped before the new shape lands, and during a staged rollout one backend can
receive both shapes from different hosts.

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

Two families of instruments, all under the `kirocrew.` namespace:

- **`kirocrew.process.*`** — this process's own resource behavior: Python and OS
  thread counts, open file descriptors, current and peak RSS, cumulative CPU
  seconds, and per-generation GC counters.
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

### Before you build a per-host dashboard

One limitation to know up front, because it will bite a dashboard rather than
announce itself. Separating machines depends entirely on the resource attributes,
and today the only identity there comes from the OpenTelemetry SDK's own detector:
a `service.instance.id` that is **generated fresh for each process**. Two hosts are
therefore distinguishable at any given moment, but a single host's series *restarts
whenever its gateway restarts*, so a longitudinal "this machine over time" panel
will show a new series after every restart rather than one continuous line.

That id comes from the SDK's default resource, so whether you have it at all depends
on your installed SDK: current versions supply it (including `1.44.0`, the version
pinned for the `kirocrew[otlp]` extra), while the declared floor is
`opentelemetry-sdk>=1,<2` and an older SDK in that range may contribute none — in
which case two hosts are not separable either. Check yours before building on it:

```bash
python -c "from opentelemetry.sdk.resources import Resource; print(Resource.create({}).attributes)"
```

This applies to every `kirocrew.*` metric, not just the inventory family, and it is
closed by giving the resource a persisted install-scoped identity. Until then,
prefer panels that group by the current instance and read point-in-time state, and
treat cross-restart continuity as unavailable.

### What these can and cannot tell you

They describe **the machines you run and have opted in**, and nothing wider. Both
switches default off and OTLP additionally needs the `kirocrew[otlp]` extra, so an
aggregate over these gauges is a statement about your own fleet — useful for
spotting a host that stopped scheduling crons or whose skills tree drifted, not a
measurement of how a feature is used in general. Kiro Crew's own install analytics
are a separate, deliberately unrelated channel (`beacon.py`), for reasons its
docstring sets out.

One practical consequence: `kirocrew.inventory.*` comes from the **gateway process
only**, while `kirocrew.process.*` comes from every telemetry-enabled process
(gateway, MCP gateway daemon, spawned agents). Install-level facts are identical
across those processes, so publishing them from each one would count a single host
once per process. If you see process metrics from a host but no inventory metrics,
the gateway on that host is not running or not exporting — that is the signal, not
a gap in collection.

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
temporality. The second stands up an in-process OTLP receiver on loopback, exports
to it with the real OTLP exporter, and decodes the protobuf — that tier skips
unless `kirocrew[otlp]` is installed.

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
`kirocrew.inventory.*` families and the resource attributes described above.

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
python3 -m json.tool < ~/.kiro/crew/metrics/metrics-*.jsonl | head -40
```

If instruments appear there but not at your backend, the problem is the endpoint,
the collector, or temporality — not collection.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No `telemetry OTLP export active` line | `otlp_endpoint` empty, or `kirocrew[otlp]` not installed — check for the warning naming the missing extra. |
| `OTLP exporter init failed` warning | Malformed endpoint. The message deliberately omits the URL, since it can carry a credential. |
| Local shards fill, nothing at the backend | Endpoint missing `/v1/metrics`, collector on 4317 (gRPC) instead of 4318 (HTTP), or the collector's `http` protocol not enabled. |
| Metrics arrive but counters look like resets | Temporality mismatch. See the temporality section. |
| `unknown type: "awsemf"` at collector startup | Running the core `otelcol`; vendor exporters need `otelcol-contrib`. |
| One expected series is absent | That probe could not read its source. Absence is a gap, never a zero. |
| Nothing recorded at all | Collection consent: `telemetry.enabled` is `false` and `KIROCREW_TELEMETRY` is unset. |
