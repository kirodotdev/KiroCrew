# Exporting chat and workflow traces for AWS evaluations

Kiro Crew includes two **optional, separately installed adapters** for exporting
chat and workflow traces to an external OpenTelemetry Collector. This is not a
native tracing switch in Kiro Crew's telemetry SDK: built-in telemetry remains
metrics-only, as described in
[Exporting Kiro Crew metrics over OTLP](telemetry-otlp-export.md). The adapters
do not install or configure a Collector.

The companion integration repository owns Collector configuration and
deployment, AWS evaluation data and runners, and Spec Builder-only legacy
validation. This guide covers only the Kiro Crew adapter and Collector handoff;
it contains no Collector configuration, AWS credentials, dataset, or evaluation
runner instructions.

## Choose an adapter

- **Hook adapter:** exports chat and tool spans from Kiro Crew hook events. It
  needs no ACP shim. The installer can also enable the ACP converter after each
  `Stop` event.
- **ACP tap and converter:** the tap observes the ACP stdio stream without
  changing it and writes local frames; `acp-to-spans.py` turns completed turns
  and workflow records into traces. Use this when you need ACP-derived assistant
  output, tool details, or workflow-agent attribution. The ACP tap requires
  Node.js; the hook runner and converter use the Python standard library.

When `--acp` is enabled, the installer sets `KIRO_OTEL_HOOK_TOOLS=off` so the
hook adapter and ACP converter do not both emit tool spans. Without `--acp`, the
hook adapter's tool spans remain enabled by default.

## Opt in or disable the hook adapter

Run these commands from a Kiro Crew checkout. The installer edits the selected
hooks JSON file; it does not change Kiro Crew's built-in metrics settings.

```sh
python3 scripts/otel/aws-evals/hook-runner-v2/install-v2.py enable \
  --hooks ~/.kiro/crew/hooks.json \
  --endpoint http://127.0.0.1:14318 \
  --service kiro-linux-v2
```

`--hooks` defaults to `~/.kiro/crew/hooks.json`. `--endpoint` is the Collector's
base URL; the adapter appends `/v1/traces`, so do not include that path. The
default is `http://127.0.0.1:14318`. `--service` sets the `service.name`
resource attribute; its installer default is `kiro-linux-v2`.

The enable operation creates a timestamped `hooks.json.before-v2-enable-*.bak`
backup, disables existing `otel-*` entries without deleting them, and adds five
enabled `otelv2-*` entries. Their commands end in `; exit 0`, so telemetry
failures do not fail the hooked operation. A complete repeated enable is a
no-op. Other hook entries are left alone.

Disable the adapter with:

```sh
python3 scripts/otel/aws-evals/hook-runner-v2/install-v2.py disable \
  --hooks ~/.kiro/crew/hooks.json
```

Disable also creates a backup, removes `otelv2-*` entries, and restores the
legacy `otel-*` enabled values from the latest enable backup. If there is no
enable backup, it leaves those legacy values as they are. It does not remove
other hooks or backups; keep or remove backups according to your own rollback
policy.

### Include workflow records and ACP spans

Add `--acp` to `enable` to run the converter after each `Stop`. Point `--runs-dir`
to the workflow-run directory used by this Kiro Crew installation and readable
by the process running the hook. The installer's default is
`/var/lib/otelcol/workflow-runs`; do not assume that is your configured Kiro Crew
store.

```sh
python3 scripts/otel/aws-evals/hook-runner-v2/install-v2.py enable \
  --hooks ~/.kiro/crew/hooks.json \
  --endpoint http://127.0.0.1:14318 \
  --service kiro-linux-v2 \
  --acp \
  --runs-dir /path/to/kirocrew/workflow-runs
```

The converter also runs as a standalone command. Its `--runs-dir` defaults to
`~/workflows/runs` (not the installer's default), so pass the configured path
explicitly when running it yourself. The service name is supplied through
`KIRO_OTEL_SERVICE_NAME`; the converter does **not** accept a `--service` option.
As standalone processes, the hook runner defaults to service `kiro-linux` and
the converter to `kiro-linux-acp`; the installer overrides both with its
`--service` value. The hook runner's separate workflow-record lookup uses
`KIRO_OTEL_RUNS_DIR` (default `~/workflows/runs`). The installer's `--runs-dir`
sets `KIRO_OTEL_WORKFLOW_RUNS_DIR` for the ACP converter; it does not set
`KIRO_OTEL_RUNS_DIR` for the hook runner.
The converter's other options include `--frames`, `--prompts`, `--since` (240
minutes by default), `--state`, `--session-map`, `--grace`, `--all-sessions`,
and `--force`.
`--force` bypasses retained-ID replay suppression and may intentionally
re-export spans.

```sh
KIRO_OTEL_ENDPOINT=http://127.0.0.1:14318 \
KIRO_OTEL_SERVICE_NAME=kiro-linux-v2 \
python3 scripts/otel/aws-evals/acp-tap/acp-to-spans.py \
  --runs-dir /path/to/kirocrew/workflow-runs
```

The installer runs the hook process with `KIRO_OTEL_ENDPOINT` and
`KIRO_OTEL_SERVICE_NAME` set from its options. It passes the same service name
to the converter when `--acp` is enabled. Do not put credentials in the
endpoint: the installer stores that URL in the hooks file, and these adapters do
not provide an authentication-header option. Use an appropriately protected
Collector endpoint or a protected local forwarding Collector instead.

## Optional ACP tap shims

The tap is not installed by the hook installer. Choose the shim for the ACP
backend you use, generate it from the checkout, then point Kiro Crew at the
generated path.

For `codex-acp`, `make-shim.sh [output-path]` defaults to
`shim/@agentclientprotocol/codex-acp/dist/index.js` beside the script. It creates
the expected package-shaped path; set `CODEX_ACP_BIN` to that entry point:

```sh
scripts/otel/aws-evals/acp-tap/make-shim.sh
CODEX_ACP_BIN="$PWD/scripts/otel/aws-evals/acp-tap/shim/@agentclientprotocol/codex-acp/dist/index.js" \
  kirocrew gateway
```

For Kiro CLI, `make-kiro-shim.sh [path-to-real-kiro-cli]` defaults to
`~/.local/bin/kiro-cli`. The real binary must be executable, and Node.js must be
on `PATH`. The generated wrapper is `scripts/otel/aws-evals/acp-tap/shim/kiro-cli`;
set `KIROCREW_KIRO_BIN` to that wrapper:

```sh
scripts/otel/aws-evals/acp-tap/make-kiro-shim.sh /absolute/path/to/kiro-cli
KIROCREW_KIRO_BIN="$PWD/scripts/otel/aws-evals/acp-tap/shim/kiro-cli" \
  kirocrew gateway
```

Both generated shims default `ACP_TAP_CONTENT` to `1`, unless it is explicitly
set to `0`. This captures redacted and bounded tool input/output, tool titles,
and assistant message text in the local ACP frame file. Prompt text is a
separate capture, also enabled by the generated shims unless
`ACP_TAP_PROMPTS=0`; it writes to its own file. These switches are independent:
`ACP_TAP_CONTENT=0` does not enable or disable prompt capture, and
`ACP_TAP_PROMPTS=0` does not disable other content capture. Prompt capture keeps
only the text after the configured marker by default; without the marker it
records no prompt text. `ACP_TAP_PROMPTS_FULL=1` changes that fallback to retain
the full prompt text. `ACP_TAP_DISABLE=1` disables tap recording altogether.

The frame file defaults to `~/.kiro/otel-traces/acp-frames.jsonl`; the separate
prompt file defaults to `~/.kiro/otel-traces/acp-prompts.jsonl`. Set
`ACP_TAP_OUT` or `ACP_TAP_PROMPTS_OUT` to choose different paths. Content is
redacted before truncation for common AWS access-key, bearer-token,
password/token/secret/API-key patterns. The default per-field content limit is
8,192 characters (`ACP_TAP_MAX_FIELD`); prompt text is separately bounded and
hard-capped at 8,192 characters (`ACP_TAP_MAX_PROMPT`). These pattern
redactions are best-effort, not a secret detector: review your data-handling
requirements before enabling capture, and do not use capture as a way to send
secrets to a Collector.

The hook adapter also exports prompt and assistant text from its hook events and,
with its default `KIRO_OTEL_HOOK_TOOLS=on`, tool input and output. It applies the
same redaction patterns and 8,192-character text bound. `--acp` changes the
tool-span source only; it does not turn off prompt or assistant content from the
hook adapter. The installer has no prompt-content opt-out, so enable it only
where that content may be exported. The ACP tap's `ACP_TAP_CONTENT` and
`ACP_TAP_PROMPTS` controls apply to the ACP frame files, not to the hook
adapter's spans.

## Correlation, state, and platform requirements

The hook adapter requires producer `event_id` and `tool_call_id` values for
tool-event identity. It joins start/result records using scoped explicit IDs;
it does not merge records by timestamp, tool name, or content. The ACP converter
also joins workflow, agent, session, turn, and tool records by explicit IDs.
Timestamps set span bounds and pending-call expiry; timestamp-based merging is
not implemented. If an explicit timestamp fallback is added in the future, it
must remain a fallback for missing IDs and must never override usable IDs.

Replay suppression is exact-ID and bounded, not permanent. The hook adapter
retains the newest 10,000 producer event IDs. The ACP converter retains 5,000
emitted-turn IDs and 1,000 workflow-run IDs. A duplicate can therefore be
emitted again after its ID has been evicted from the applicable window; unseen
records are not rejected due to timestamp ordering. The converter's `--since`
option is a separate input-window filter. State is stored locally under
`~/.kiro/otel-traces/state-v2` by default. Both adapters use `fcntl.flock` for
state coordination, so these optional exporters require a POSIX platform such
as Linux or macOS and are not supported on native Windows.

For an AgentCore evaluation session, identity selection prefers
`parent_session_key` only when it starts with the exact root prefix
`dashboard:agentcore-eval-`; otherwise it uses the event's own `session_key`,
then the legacy `session_id`. The own and parent keys are retained as separate
span attributes. The exporters preserve the selected key: they do not strip the
prefix. The external Collector must remove exactly
`dashboard:agentcore-eval-` from matching evaluation `session.id` values so the
final ID equals the AgentCore scenario ID; ordinary session IDs should pass
through unchanged. The ACP converter's older `--session-map` lookup is only a
compatibility fallback when those event identity fields are absent.

For ordinary dashboard turns without a producer turn ID, the ACP tap snapshots
the active hook's exact trace and span IDs. Ambiguous session ownership is not
resolved by timestamps. Chat-launched workflows are linked through the exact
run ID returned by `workflow_run` and the launching ACP turn's hook IDs. Failed
or cancelled workflows also retain tool calls from unfinished ACP prompts;
interrupted calls are marked partial and failed/cancelled, not successful.

Before attempting network delivery, exports are synced to a durable
`export-outbox` below `KIRO_OTEL_TRACE_DIR` (default
`~/.kiro/otel-traces`). Pending envelopes keep their original trace/span IDs and
are retried on subsequent converter passes, including passes with no new spans.
Collector rejection or connection failure leaves the envelope queued. Keep this
directory on persistent storage and monitor disk usage during prolonged outages.
Delivery is at least once: a crash after acceptance but before queue cleanup can
resend the same span IDs. A dry-run writes only to its configured output file
and never drains queued network exports.

## External Collector contract for AWS traces

Configure an external Collector to accept OTLP/HTTP traces at the endpoint
above and export them to the CloudWatch Transaction Search destination used by
your integration. The Collector, not these adapters, supplies the AWS-specific
resource attribute `aws.service.type=gen_ai_agent` and the CloudWatch routing.
The configured `service.name` must match the service name selected at install
time; the integration commonly uses `kiro-linux-v2`. The destination log group
is `aws/spans`.

Preserve `session.id` and each span's `traceId`, `spanId`, and applicable
`parentSpanId` so session grouping and trace parentage survive export. Validate
that child spans remain attached to the intended parent and that the final
normalized `session.id` is the scenario ID, rather than the prefixed Kiro Crew
slot key. Those are Collector/backend integration checks; this repository does
not ship a Collector config, deployment, credentials, evaluation dataset, or
runner.

## Separate from metrics export

These scripts are optional standalone adapters, not a general Kiro Crew native
tracer and not an extension of `telemetry.otlp_endpoint`. The built-in metrics
path remains the separate, metrics-only feature described in
[Exporting Kiro Crew metrics over OTLP](telemetry-otlp-export.md). Installing
these scripts does not turn native metric instrumentation into trace
instrumentation, and setting up metrics export does not install these adapters.
