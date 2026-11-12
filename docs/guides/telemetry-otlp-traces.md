# Sending Kiro Crew traces to Langfuse, CloudWatch or any OTLP collector

Kiro Crew can record one OpenTelemetry trace per agent turn: an `invoke_agent`
span for the turn, a `chat` span with the model, credits and tokens, and one
`execute_tool` span per tool call. Sub-agent runs nest under the turn that
started them. This guide turns that on and sends it to a backend.

## Default posture

- `telemetry.traces_enabled` is `false`. Off loads nothing and records nothing.
- On, spans go to local JSONL files under `~/.kiro/crew/traces` only.
- Spans leave the host only when you also name a collector, and only while the
  switch is on. An `OTEL_EXPORTER_OTLP_ENDPOINT` set for another tool does
  nothing while the switch is off.
- Never on a span: prompt or reply text, tool arguments or output, file paths, MCP
  server or tool names (an MCP call is named `execute_tool mcp`), and secrets.
  The raw session key stays in the local files; the wire gets a keyed hash.
- An LTS build ignores every setting on this page that names a collector.
- As for metrics, the standard `OTEL_RESOURCE_ATTRIBUTES` variable, when set in
  the gateway's environment, adds its keys to the trace resource that is sent.

## Turning it on

1. Install the exporter, the same extra the metrics export uses:

   ```bash
   pip install "opentelemetry-exporter-otlp-proto-http==1.44.0"
   ```

2. Store the backend's auth header as a secret: dashboard **Settings → Secrets**,
   any name (this guide uses `TRACES_OTLP_AUTH`), value = the **whole header
   value**, for example `Basic dXNlcjpwYXNz…` or `Bearer …`. Config only ever holds
   the entry's name.

3. Set these in `~/.kiro/crew/config.json`:

   ```json
   {
     "telemetry": {
       "traces_enabled": true,
       "traces_otlp_endpoint": "https://<backend>/v1/traces",
       "traces_otlp_headers_secret": {"Authorization": "TRACES_OTLP_AUTH"}
     }
   }
   ```

   A settings write takes effect on the next turn; no restart.

Rules the endpoint must meet, or export stays off with one warning in the log:

- `https://`, unless the host is loopback (`127.0.0.1`, `::1`, `localhost`);
- no `user:password@`, query string or fragment: put credentials in a header;
- a host the `capabilities.telemetry` `otlp_hosts` policy permits, when a policy
  is installed. The decision is written to the security event log, and a policy
  installed later stops the next export.

A header value written straight into `traces_otlp_headers_secret` is refused, not
sent. One known limitation: a collector that echoes credentials back (for example
as a malformed response header) can cause them to appear in the gateway's local
log, because the HTTP library logs such a line as it reads it. Use a collector
you trust. `traces_otlp_headers_env` maps a header to an environment variable instead,
like a metrics destination's `headers_env`. No header value is ever logged.

**Environment variables.** With `traces_otlp_endpoint` empty, Kiro Crew reads
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, then `OTEL_EXPORTER_OTLP_ENDPOINT` with
`/v1/traces` appended. That endpoint gets `OTEL_EXPORTER_OTLP_TRACES_HEADERS` (else
`OTEL_EXPORTER_OTLP_HEADERS`). The config endpoint never gets those variables, so a
credential set for another collector is never sent to yours. `OTEL_SERVICE_NAME`
renames the trace resource only; metrics keep `service.name=kirocrew`.

## Langfuse

Langfuse takes OTLP/HTTP at `/api/public/otel/v1/traces` with Basic auth made from
your project's public and secret keys
([Langfuse OpenTelemetry](https://langfuse.com/docs/opentelemetry/get-started)).

1. Build the header value: `Basic ` + base64 of `<public key>:<secret key>`.

   ```bash
   printf 'Basic %s\n' "$(printf '%s' 'pk-lf-…:sk-lf-…' | base64 | tr -d '\n')"
   ```

2. Store it as `TRACES_OTLP_AUTH` in Settings → Secrets.
3. Set `traces_otlp_endpoint` to your region's URL, for example
   `https://cloud.langfuse.com/api/public/otel/v1/traces` (EU) or
   `https://us.cloud.langfuse.com/api/public/otel/v1/traces` (US), and the header
   map from step 3 above.

Each `chat` span is a Langfuse **generation**: it carries
`langfuse.observation.type=generation` and `langfuse.observation.usage_details`.
A Kiro turn reports credits as a usage type named `credits`; a Claude or opencode
turn reports tokens and `cost_details`. Kiro Crew ships no credits-to-USD rate,
because credit prices differ by plan. To see a cost, add a Langfuse model
definition with a price for the `credits` usage type. Traces group into sessions
by `session.id`, a keyed hash of the Crew session.

## AWS CloudWatch (Transaction Search)

X-Ray's OTLP endpoint is `https://xray.<region>.amazonaws.com/v1/traces`. It needs
Transaction Search enabled in the account and region, and accepts HTTP only
([CloudWatch OTLP endpoints](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html)).

### With an X-Ray API key

[X-Ray API keys](https://docs.aws.amazon.com/xray/latest/devguide/xray-opentelemetry-bearer-token.html)
are sent as a bearer token. The key is a long-term credential: give it an expiry,
keep it only in the vault, and rotate it.

1. Store `Bearer <api key>` as `TRACES_OTLP_AUTH` in Settings → Secrets.
2. Set `traces_otlp_endpoint` to `https://xray.us-east-1.amazonaws.com/v1/traces`
   (your region) and `traces_otlp_headers_secret` to
   `{"Authorization": "TRACES_OTLP_AUTH"}`.

The header is read when the exporter is built. After rotating the key, turn
`traces_enabled` off and on again, or restart the gateway. Saving another
setting does not re-read it.

### Through a collector with SigV4

If you run the ADOT Collector or an OpenTelemetry Collector with the
`sigv4auth` extension, it signs with its own AWS credential chain and Kiro Crew
needs no credential at all. Point Kiro Crew at the collector over loopback:

```json
{"telemetry": {"traces_enabled": true,
               "traces_otlp_endpoint": "http://127.0.0.1:4318/v1/traces"}}
```

```yaml
extensions:
  sigv4auth:
    region: us-east-1
    service: xray
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318
exporters:
  otlphttp:
    traces_endpoint: https://xray.us-east-1.amazonaws.com/v1/traces
    auth:
      authenticator: sigv4auth
service:
  extensions: [sigv4auth]
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlphttp]
```

Native SigV4 signing inside Kiro Crew is planned separately.

### Credits in CloudWatch

Model and token counts use the GenAI conventions (`gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) where the harness
reports them. Kiro credits are a `kirocrew.usage.credits` attribute. Transaction
Search stores spans in the `aws/spans` log group, so Logs Insights can sum them.
Filter to `chat` spans, because the same spend is also on each `invoke_agent`:

```
fields @timestamp, attributes.session.id
| filter name like /^chat/
| stats sum(attributes.kirocrew.usage.credits) as credits by bin(1d)
```

Check the field names against one span in your log group first; this query has
not been run against a live account by the Kiro Crew maintainers yet.

## Checking it works

- `~/.kiro/crew/traces/spans-*.jsonl` fills as soon as the switch is on, with or
  without a collector.
- With a collector named, the gateway log is quiet when export works. Each problem
  logs one warning naming the setting or header, never its value: a refused
  endpoint, a missing secret, the exporter extra not installed, or a host the
  policy refuses.
- Batches go out every 5 seconds and at shutdown. A slow collector never delays a
  turn: spans queue (up to 2,048) and the oldest are dropped past that.
