# Inbound Webhooks

Lets an external system trigger an agent turn over HTTP. A CI runner, a code
review bot, a ticket system, or a shell script POSTs a message to
`/api/hooks/agent`; the gateway runs one agent turn in an ephemeral session and
delivers the answer to your notifications, not back over the HTTP connection.

This is the inbound counterpart to cron jobs: cron fires on a schedule Kiro Crew
owns, a webhook fires when something outside Kiro Crew decides it is time.

## The fire-and-forget contract

The endpoint accepts, queues, and returns. It never carries the agent's answer.

```
POST /api/hooks/agent
  ├─ token check                                    → 401 on failure
  ├─ bounded raw-body read (256 KiB)                → 413 when exceeded
  ├─ HMAC + replay check (when required)            → 401 on failure
  ├─ payload validation (message, sessionKey, …)    → 400 on failure
  ├─ capacity check (6 concurrent)                  → 429 when full
  └─ spawn background task, respond immediately
       {"status": "accepted", "sessionKey": "hook:review:pr-123"}

     … background, up to timeoutSeconds …
       ├─ prepend registered context (if any, and fresh enough)
       ├─ run one agent turn
       ├─ destroy the session
       └─ deliver the result → dashboard notification + Slack DM (owner)
```

Two consequences worth internalising before you build against it:

- **The HTTP response tells you nothing about the outcome.** A `200` means the
  turn was accepted. Success, timeout, and failure all look identical to the
  caller. Watch the notification feed (or the Webhooks page's run list) for the
  outcome.
- **The turn is unattended.** No one approves tool calls on the agent's behalf,
  so the turn runs with whatever approval policy the gateway was started with.

## Request

```bash
curl -X POST http://127.0.0.1:5476/api/hooks/agent \
  -H "Origin: http://127.0.0.1:5476" \
  -H "Authorization: Bearer $KIROCREW_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "message": "Static analysis finished on PR 123. 2 highs, 1 medium.",
        "sessionKey": "hook:review:pr-123",
        "name": "Review Bot",
        "deliver": true,
        "timeoutSeconds": 900
      }'
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `message` | string | — | Required, non-empty after trimming. Max 49,999 characters. |
| `sessionKey` | string | `hook:default:<unix-ts>` | Must start with `hook:`. The part after the prefix is the **hook id** used to look up registered context. |
| `name` | string | `Webhook` | Human label shown in the notification title. |
| `agent` | string | gateway default | Route the turn to a named agent. |
| `deliver` | boolean | `true` | When false, the turn runs and is logged but nothing is pushed to you. |
| `timeoutSeconds` | integer | `599` | Clamped to the range 60–3593. Values outside it are silently clamped, not rejected; a non-integer is a 400. |

The `Authorization: Bearer <token>` header is the documented form.
`X-KiroCrew-Token: <token>` is accepted as an equivalent.

### Responses

| Status | Body | Cause |
|---|---|---|
| `200` | `{"status": "accepted", "sessionKey": "…"}` | Turn queued. |
| `400` | `{"error": "…"}` | Malformed JSON, empty `message`, message over 49,999 chars, `sessionKey` without the `hook:` prefix, or a non-integer `timeoutSeconds`. |
| `401` | `{"error": "unauthorized"}` | No matching token — including the case where no token has been configured at all. |
| `401` | `{"error": "…"}` | Signing failure on a token that requires signatures: `X-KiroCrew-Timestamp` or `X-KiroCrew-Signature` missing, timestamp unparseable or more than 300 seconds from now, digest mismatch, or a signature already seen inside the window (replay). Each cause has its own `error` string. |
| `403` | `{"error": "Forbidden"}` | Not produced by this endpoint any more. If you see it, you are hitting a different path or a proxy in front of the gateway. || `413` | `{"error": "request body exceeds 262144 bytes"}` | The raw request body is larger than the endpoint-local 256 KiB limit. Fixed-length and chunked bodies are both bounded before JSON parsing. |
| `429` | `{"error": "hook capacity reached (6)"}` | All six concurrent slots are in use. Retry with backoff. |
| `429` | `{"error": "too many failed attempts"}` | This source sent 10 failed authentications — bad tokens or bad signatures — within a minute, and is blocked for five. |
| `503` | `{"error": "inbound webhooks are disabled"}` | The kill switch is off. Returned before the token is checked. |

Note that the accept response is `200`, not `201` or `202`.

## Authentication

The webhook token is the only thing this endpoint checks **about the caller's
identity**. There is no second network gate, no allowlist, and no per-caller
scope.

One check does run before the token, though: the CSRF origin check applied to
every non-safe method. A request with no `Origin` header is trusted only when it
arrives from loopback — which covers the SSH-tunnel setup below, since the
gateway then sees the connection as coming from `127.0.0.1`. A caller reaching a
publicly bound gateway without an `Origin` header is rejected with `403` before
its token is ever examined. Send `Origin: <scheme>://<host>:<port>` of the
gateway itself and the check passes in both cases; the examples below do.

Tokens are managed on the Webhooks page. Each one carries a label so you can
tell callers apart ("Review Bot", "Deploy pipeline") and revoke one without
breaking the others.

- The raw secret is shown **exactly once**, at creation. Only its SHA-256 hash
  is persisted, so a lost token cannot be recovered — revoke it and issue a new
  one.
- Verification is a constant-time comparison. The token's last-used time is
  stamped only after every required HMAC and replay check also passes; a bad
  signature never makes a credential look successfully used. That timestamp is
  the cheapest way to tell whether an integration you set up months ago is still
  calling.
- A legacy single token set as `hooks.webhook_token` in `config.json` continues
  to work, and appears in the list as a read-only entry. To remove it, delete
  the key from the config file — Kiro Crew does not rewrite your config for you.
- Webhooks are **disabled by default**: with no token configured, every request
  is a 401. There is no unauthenticated mode.
- A source that keeps presenting bad tokens is throttled — 10 failures in a
  minute earns a `429` for five minutes. The source is the immediate TCP peer;
  untrusted forwarded-address headers are deliberately ignored. Consequently,
  callers behind one reverse proxy share a throttle bucket. Configure a trusted
  proxy's own per-client rate limit if that deployment needs isolation. This is
  an abuse damper, not a security boundary; the boundary is the token itself.
- A token can additionally **require a signed request**, which is the default for
  newly minted tokens. See [Request signing](#request-signing).

`POST /api/hooks/agent` is on the auth middleware's bypass list, in the same
self-authenticating-external-webhook class as `/api/messaging/teams`. It is not
a strict internal path: an external caller needs the bearer token and nothing
else — no dashboard cookie, no gateway IPC secret.

The bypass is scoped to **POST**, and only that method. The literal path also
matches the `{hook_id}` wildcard of the dashboard's own hook CRUD routes
(`PUT`/`DELETE /api/hooks/{hook_id}`), which authenticate on the dashboard token
alone — so every method other than POST stays behind the ordinary gate.

### What actually limits reach

Because auth is the token alone, **the bind address is what decides who can
attempt a call**. By default the gateway binds loopback only, so an external
system reaches it through an SSH tunnel:

```bash
ssh -NL 6776:127.0.0.1:6776 <gateway-host>
```

Binding a public interface instead places a remote-execution credential directly
on the network. Prefer the tunnel, or a reverse proxy that terminates TLS and
adds its own access controls.

## Request signing

The bearer token answers one question: *who is calling*. It says nothing about
whether the body arrived as it was sent, and a captured request stays valid
forever. HMAC request signing closes both gaps.

A signing token is minted with a second secret and, from then on, its calls must
carry two extra headers:

```
X-KiroCrew-Timestamp: 1785372000            # unix seconds, integer
X-KiroCrew-Signature: sha256=<hex hmac>     # lowercase hex
```

The signed string is exactly:

```
{timestamp}.{raw request body}
```

A literal `.` between the timestamp and the body, and the **raw bytes as sent**,
before any JSON parsing. Re-serialising the parsed body — reordering keys,
changing whitespace, dropping a trailing newline — produces a different MAC and
the call is rejected. Sign the exact string you put on the wire.

Verification runs in this order, and every failure is a `401` whose `error` names
the specific cause:

1. The bearer token resolves to a token entry. (Unchanged — a bad bearer never
   gets as far as the signature.)
2. If that token requires signatures, both headers must be present.
3. The timestamp must parse as an integer and fall within **±300 seconds** of the
   gateway's clock.
4. The digest must match, compared in constant time against the HMAC-SHA256 of
   the signed string keyed with that token's signing secret.
5. The exact signature must not have been seen before inside the window. A
   captured request cannot be replayed even while its timestamp is still valid.

The seen-signature set is held **in memory, per gateway process**, so its scope
is worth stating exactly: within the life of the process that accepted a request,
that signature is refused for the rest of the window. If the gateway restarts
inside those 300 seconds, the set starts empty, and a captured request whose
timestamp is still valid could be accepted once more. The absolute bound on
replay is therefore the ±300-second timestamp window, not the seen-set; the set
narrows it to once-per-process within that window. Persisting the set would put a
disk write on the path of every accepted call, which is why it is in memory —
if that tradeoff is wrong for your deployment, the window is the knob to shorten.

The set holds at most 4,096 entries. If that many *distinct* signed calls arrive
inside one window, further signed calls are refused with `replay protection
saturated, retry shortly` rather than the oldest live entry being dropped:
forgetting a signature the window would still accept is how replay protection
fails open, so the endpoint sheds load instead. Reaching this needs roughly 13
signed calls per second sustained for the whole window, well past the six
concurrent runs the endpoint will execute.

Signature failures feed the same per-source throttle as bad bearers, so a caller
that is signing wrongly earns the `429` after ten attempts in a minute rather
than looping forever.

### The tradeoff, plainly

Verifying an HMAC means the gateway must be able to **recompute** it, so the
signing secret is stored retrievably in `webhook_tokens.json` (0600, same file as
the token metadata). That is weaker at rest than the bearer token, which is
stored only as a SHA-256 hash and cannot be read back out of the file at all.

Both mechanisms ship because they prove different things:

- The **bearer token** proves who is calling, and is safe at rest.
- The **signature** proves the body was not altered and is not a replay, at the
  cost of a recoverable secret on disk.

Treat the signing secret with the same care as the token: it is shown once at
creation and is not recoverable afterwards.

### Per-token control

- `require_signature` defaults to **on** for every newly minted token. Mint a
  bearer-only token explicitly when a caller cannot sign.
- The signing secret is returned **once**, in the create response, next to the
  bearer token. It is never included in the tokens list the dashboard reads.
- The legacy `hooks.webhook_token` config scalar has no signing secret, so it
  stays bearer-only. Existing installs keep working unchanged.

### Signing a call from bash

```bash
TS=$(date +%s)
BODY='{"message":"Static analysis finished on PR 123.","sessionKey":"hook:review:pr-123"}'
SIG=$(printf '%s.%s' "$TS" "$BODY" \
  | openssl dgst -sha256 -hmac "$KIROCREW_WEBHOOK_SIGNING_SECRET" -hex \
  | sed 's/^.* //')

curl -X POST http://127.0.0.1:5476/api/hooks/agent \
  -H "Origin: http://127.0.0.1:5476" \
  -H "Authorization: Bearer $KIROCREW_WEBHOOK_TOKEN" \
  -H "X-KiroCrew-Timestamp: $TS" \
  -H "X-KiroCrew-Signature: sha256=$SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

`printf` rather than `echo` matters: `echo` appends a newline that is not part of
the body curl sends, and the MAC would not match. Build the body once, into a
variable, and hand the same bytes to both the digest and the request.

## Turning webhooks off

The Webhooks page carries a kill switch. Off means **no inbound call is
accepted**, regardless of which token it presents. The switch defaults to on, so
a fresh install is closed by having no tokens rather than by the switch, and an
upgrade that already had a token keeps working without touching it.

The switch is checked before any authentication work, so while it is off a call
gets a `503` and its token is never examined — a disabled endpoint cannot be
probed to find out which tokens are valid. The rejection is still recorded in the
run list, so you can see that something tried.

Turning it off is **non-destructive**. Tokens, their labels and last-used stamps,
registered `register_hook` contexts, and the run history all survive. Turning it
back on restores every integration exactly as it was — nothing needs
re-provisioning, and no caller needs a new secret.

Two states are worth distinguishing on the page: the switch being off, and the
switch being on with no tokens configured. Both refuse every call, but only the
first is reversible with one click; the second needs a token.

## Limits

| Limit | Value | Behaviour at the limit |
|---|---|---|
| Raw request body | 256 KiB (262,144 bytes) | `413`; reading stops after one byte beyond the cap |
| Message length | 49,999 characters | `400` |
| Concurrent runs | 6 | `429`, request is not queued |
| Turn timeout — default | 599 seconds | Turn is abandoned, outcome recorded as a timeout |
| Turn timeout — maximum | 3593 seconds | Larger requests are clamped down to it |
| Turn timeout — minimum | 60 seconds | Smaller requests are clamped up to it |
| `hook_id` (via `register_hook`) | 500 characters | Tool call is rejected |
| `context_summary` (via `register_hook`) | 5,000 characters | Tool call is rejected |
| Signature timestamp window | ±300 seconds | `401`, request is not run |
| Failed authentications per source | 10 per 60 seconds | `429` for 300 seconds |

The two timeout bounds are prime numbers on purpose — they keep repeated webhook
runs from settling into lockstep with cron intervals.

## Session lifecycle

Webhook sessions are **ephemeral**. The session is created (or an existing one
adopted) for the turn, then released and reset when the turn ends — on success,
on timeout, and on error alike. There is no conversation to come back to.

This is the same model subagents use, and it is why the `sessionKey` is a routing
key rather than a handle: two calls with the same `sessionKey` are two unrelated
turns, not a thread. Anything the second turn needs to know, the first turn has
to have written down.

When the turn ends, the result is redacted for credentials and exfiltration URLs
before it leaves the gateway. Delivery, when `deliver` is true, is a dashboard
notification (first 2,000 characters) plus a Slack DM to the owner (first 3,000
characters) when Slack is configured. An empty result is not delivered at all.

## Carrying context across calls: `register_hook`

Because sessions are destroyed, continuity is explicit. The agent calls the
`register_hook` MCP tool to write a summary of what it was doing, keyed by hook
id, and the next webhook call for that hook id gets it prepended to the incoming
message:

```
=== Restored Context (from prior session) ===
<your context_summary>
=== End Restored Context ===

<the message the external system posted>
```

`register_hook` takes `hook_id` and `context_summary` and returns the session key
(`hook:<hook_id>`) and the webhook URL to hand to the external system.
Registrations live in `~/.kiro/crew/hooks.json`, written under an exclusive lock
with an atomic replace, and are keyed by hook id — registering the same id again
overwrites the previous summary. A registration is not consumed by a call; it
keeps being injected until it is overwritten or ages out.

### Freshness horizons

How much of the stored context is injected depends on how old it is, measured
from the registration time:

| Age | What the agent receives |
|---|---|
| Under 1 hour | The summary verbatim. |
| 1 to 24 hours | The summary, prefixed with `[Context from Nh ago — may be outdated]`. Treat its claims with lower confidence and verify before acting. |
| Over 24 hours | Nothing. The context is dropped silently and the turn starts cold. |

An entry with no recorded registration time is treated as expired. Nothing
deletes aged-out entries from disk — they simply stop being injected — so a hook
that goes quiet for a week and then fires will get a fresh, contextless turn
rather than a stale one.

## Worked example: the handoff pattern

The pattern this feature exists for is a long external round-trip that would
otherwise strand the agent: submit work, let something else grind on it, get
woken back up with the answer.

**Step 1 — the agent submits work and registers a hook.**

The agent pushes a branch and opens a pull request, then calls:

```json
{
  "hook_id": "review:pr-123",
  "context_summary": "Opened PR #123 (fix/upload-limit) on kirodotdev/KiroCrew. Worktree /home/me/wt-upload-limit, branch fix/upload-limit, head 4f2b91a. Added a token-bucket limiter to api_file_upload. Pending: static analysis. When findings arrive, fix Critical/High in that worktree, amend the single commit, force-push with lease, and report what was left unfixed."
}
```

The tool returns:

```
Hook registered: review:pr-123
Session key: hook:review:pr-123
Webhook URL: http://127.0.0.1:5476/api/hooks/agent
```

The agent passes that session key to the external system — as a CI job
parameter, a PR comment, a payload field — and its turn ends. Nothing is held
open.

**Step 2 — the external system calls back.**

When analysis finishes, the bot POSTs its findings, quoting the session key it
was given:

```bash
curl -X POST http://127.0.0.1:5476/api/hooks/agent \
  -H "Origin: http://127.0.0.1:5476" \
  -H "Authorization: Bearer $KIROCREW_WEBHOOK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "message": "Analysis complete on PR 123.\nHIGH src/kiro_crew/dashboard/handlers/files.py:212 unbounded read before limit check\nMEDIUM test/test_files.py:88 missing negative case",
        "sessionKey": "hook:review:pr-123",
        "name": "Review Bot",
        "timeoutSeconds": 1800
      }'
```

**Step 3 — a fresh session resumes the work.**

The turn opens with the restored-context block, so the agent knows which
worktree, which branch, and what it had committed to doing. It fixes the
findings, force-pushes, and — because analysis will run again — calls
`register_hook` once more with an updated summary before finishing. The loop can
repeat as many times as the external system needs.

Two habits make this reliable:

- **Write the summary for a stranger.** The session that reads it has none of the
  original conversation — no file paths in scrollback, no prior tool output. Name
  the repository, the branch, the directory, and the decision that was pending.
- **Re-register on every turn** that expects another callback. Registration
  timestamps drive the freshness horizons, so a summary refreshed each round
  never crosses into the staleness banner.

## Security

A valid webhook token, once it has cleared the transport gate, buys the caller an
**arbitrary agent turn with the agent's full tool access** — shell commands, file
reads and writes, network calls, MCP tools. The `message` field is a prompt, not
data: there is no restricted vocabulary and no per-token scope. Treat a webhook
token as equivalent to shell access on the gateway host.

That is why the transport gate exists, and why you should not dismantle it:

- **Keep the gateway on loopback.** It binds to `127.0.0.1` by default. Reach it
  from elsewhere with an SSH tunnel (`ssh -NL 5476:127.0.0.1:5476 <host>`) rather
  than by widening the bind address.
- **One token per caller, revoked when the integration is retired.** The
  last-used timestamp is there so you can spot the ones nobody is calling.
- **Leave signing on** unless a caller genuinely cannot compute an HMAC. It is
  what stops a captured call from being replayed and a proxied body from being
  edited in flight.
- **Prefer `deliver: true` while you are setting an integration up.** Silent runs
  are indistinguishable from runs that never happened.
- Every call is written to the security event log — accepted, rejected for
  capacity, and denied for a bad token — so `kirocrew security events` shows you
  who has been knocking.
- The `hook:` prefix requirement is a namespace guard: it keeps a webhook caller
  from steering its turn into one of your dashboard or Slack sessions by naming
  that session's key.

## Known limitations

- **Aged-out context is never cleaned up.** Entries past the 24-hour horizon stop
  being injected but stay on disk until overwritten or removed.
- **No delivery receipt for the caller.** There is no endpoint an external system
  can poll to learn how its own call turned out; outcomes surface only in
  notifications, the run list, and the audit log.

## See also

- [Cron Jobs](cron-and-scheduling.md) — the outbound, schedule-driven counterpart
- [Configuration](configuration.md) — config file and environment reference
- [Subagents](subagents.md) — the other place ephemeral, unattended turns run
