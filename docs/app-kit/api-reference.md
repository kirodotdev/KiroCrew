# API Reference — Kiro Crew Gateway API & Client

Reference for the Kiro Crew Gateway HTTP and WebSocket APIs, and how apps consume
them.

How you talk to the Gateway depends on where your code runs:

- **Dashboard UI pages (TypeScript/React)** — use the `@kirocrew/app-sdk` hooks
  (`useAppApi`, `useAppEvents`, …). You do **not** `npm install` this package;
  the dashboard host provides it at runtime through its import map (the bare
  specifier `@kirocrew/app-sdk` resolves to the host's vendored copy via
  `window.__kirocrew_modules`). See
  [getting-started.md](getting-started.md) and the [App SDK Hooks](#app-sdk-hooks-dashboard-ui)
  section below.
- **Python apps / external CLI tools / services** — use the standalone
  `kirocrew-client` package, carried in this repository under
  `packages/kirocrew-client-py/`. It is async (`aiohttp`) and has no dependency on
  the Kiro Crew main package, but it is not published to PyPI — use it from a source
  checkout. See the [Python Client](#python-client) section.
- **Node.js / Electron apps** — call the Gateway REST/WS endpoints directly via
  `fetch()` / a WebSocket. Selected endpoint paths are in
  [Gateway REST API Endpoints](#gateway-rest-api-endpoints).

There is no published TypeScript gateway-client npm package, and none is planned
here — the camelCase names used throughout the sections below are **labels for
Gateway endpoints**, not callable methods. Read them as endpoint identifiers.
The `@kirocrew/app-sdk` hooks are real and callable — see the next section; they
resolve from the host import map. The `kirocrew-client` Python package is **not
published**: it lives in this repository under `packages/kirocrew-client-py/`, is
outside the installed distribution, and has no release on PyPI, so `pip install
kirocrew-client` does not work. Use it from a source checkout, or call the
endpoints directly with `fetch` or `aiohttp`. Its method list is in
[Python Client](#python-client).

## App SDK Hooks (dashboard UI)

Dashboard UI pages import permission-scoped hooks from `@kirocrew/app-sdk`,
resolved at runtime via the host import map:

```tsx
import { useAppApi, useAppEvents } from '@kirocrew/app-sdk'

function MyPage() {
  const api = useAppApi()        // permission-scoped GET/POST/PUT/PATCH/DELETE
  useAppEvents('notification', (e) => console.log(e))
  // ...
}
```

`useAppApi()` returns a client whose methods (`raw`, `request`, `get`, `post`,
`put`, `patch`, `del`) call the Gateway endpoints listed below, scoped to the
`permissions.api` paths your `app.json` declares. JSON methods parse a JSON
response; an empty successful response returns `undefined`.

- `raw(path, init?)` returns a successful `Response` without consuming its body.
  Use it for binary downloads, text or streamed responses and response headers.
  Non-success responses still throw `AppApiError`. Supply an `AbortSignal` for
  long-lived streams and abort or cancel the reader when the component unmounts;
  the method does not implement EventSource reconnect or SSE parsing.

- `request<T>(path, init?)` accepts `RequestInit`, including raw bodies such as
  `FormData`, headers and an abort signal. It does not set a content type for you.
- `get<T>(path, init?)` and `del<T>(path, init?)` fix the HTTP method.
- `post<T>(path, body?, init?)`, `put<T>(path, body?, init?)` and
  `patch<T>(path, body?, init?)` serialize the body argument as JSON. Their method
  and body arguments take precedence over `init.method` and `init.body`. Headers
  are merged with a default `Content-Type: application/json` unless you specify
  another media type.

The host owns `X-Session-Key`: chat surfaces use their bound session and routed
app pages use the core dashboard-page identity, `dashboard:ui`. A host-provided
key overrides a caller-supplied one. If a host has no binding, supplying that
header is rejected before a request is sent; callers of this scoped client
cannot choose a session. This is a frontend guardrail, not isolation from other
JavaScript in the dashboard document; backend authorization remains authoritative.
The path check applies to the initial URL. Browser redirect behavior remains
controlled by `RequestInit.redirect` (default `follow`); use `redirect: 'error'`
when the call must not follow redirects. Redirect targets are not rechecked by
this client.

HTTP failures are `Error` objects with the message `API <status>: <body>`. They
also carry `name: 'AppApiError'`, numeric `status` and string `body`. Import
`AppApiError` as a **type**, not a runtime constructor. The body is unparsed, so
parse it only when the endpoint promises JSON (for example, a conflict response).
Network, abort and successful-response JSON parsing failures retain their original
error types. Stale-owner reauthentication signaling still runs before an HTTP
failure is thrown.

The path matcher uses the backend's declared-pattern semantics: `/api/example`
matches itself and slash-delimited children; `/api/example/*` also includes the
base path; `/api/example*` includes any string prefix match. Blank entries match
nothing, surrounding whitespace is stripped, and request paths are normalized
before matching. A bare trailing slash is literal, not shorthand for `/*`.

An explicit authentication-expiry response (`403`, `X-Auth-Required: true`)
notifies the dashboard's existing recovery handler. It does not turn ordinary
permission denials into refresh attempts or automatically replay writes.

For the full hook list see [getting-started.md](getting-started.md#app-sdk-hooks).

## Embedded Chat

`ChatEmbed` mounts Kiro Crew's native transcript and compact composer for an
existing session. The required `slotKey` selects the session. Existing props such
as `agent`, `placeholder`, `frameless`, `startAtBottom`, `onSend`, and
`aboveComposer` keep their current contracts.

```tsx
import { ChatEmbed } from '@kirocrew/app-sdk'

<ChatEmbed slotKey="coder-abc123" />
```

The composer accepts multiple lines. `Enter` sends the draft, `Shift+Enter`
inserts a line break, and an Enter used to commit an input method editor (IME)
candidate does not send. The box grows with the draft up to 240 pixels, then
keeps its height and scrolls vertically.

A host that boxes the embed at a fixed height passes `composerMaxHeight` (in
pixels) to lower that cap, so a long draft cannot take most of the box from the
transcript. The resting (empty) size of the composer is unchanged; only the cap
moves. Omitted, the 240-pixel default applies.

```tsx
<div style={{ height: 420 }}>
  <ChatEmbed slotKey="coder-abc123" composerMaxHeight={160} />
</div>
```

## Native Chat Panel

`ChatPanel` mounts Kiro Crew's native chat experience for an existing session. The required
`slotKey` selects the session. By default, the component keeps the standard embedded ChatPage
behavior.

```tsx
import { ChatPanel } from '@kirocrew/app-sdk'

<ChatPanel slotKey="coder-abc123" />
```

Set `conversationOnly` when the host app already provides navigation and needs the conversation
without ChatPage's sessions rail. This mode keeps the native transcript, composer, and composer
controls, and it leaves the host page in charge of the browser URL.

```tsx
<ChatPanel slotKey="coder-abc123" conversationOnly />
```

| Prop | Type | Required | Purpose |
|---|---|---|---|
| `slotKey` | `string` | yes | Select the Kiro Crew session rendered by the panel |
| `conversationOnly` | `boolean` | no | Hide ChatPage's sessions rail and disable ChatPage URL synchronization |

## Chat Marker Protocol

An agent encodes UI affordances inline in the prose it streams. A surface that renders a transcript
has to interpret them, because the backend deliberately leaves the complete marker in the stream for
a frontend consumer to extract:

| Marker | Meaning |
|---|---|
| `[OPTIONS: a \| b]` | follow-up choices, several may be picked |
| `[OPTION: a \| b]` | follow-up choices, one only |
| `[STEERING steer-<id>: …]` | the agent acknowledging a mid-turn steer |

Two failure modes matter, and both are the consumer's responsibility. Render the text unparsed and
the user reads machine syntax. Strip the marker without offering the choices and the user's options
are **deleted** — worse than leaving them visible, because the text is gone too.

The parsers live in one React-free module so every surface reads the protocol from the same place:

```
website/src/app-sdk/protocol/
  optionMarker.ts   the marker pattern (in-tree only) + stripPartialOptionMarker
  options.ts        parseOptions, deriveFollowUpOptions
  steering.ts       extractSteeringAcks
```

### Using it from an app

Apps resolve `@kirocrew/app-sdk` through the host import map, the same way they get the hooks:

```tsx
import { parseOptions, extractSteeringAcks, deriveFollowUpOptions } from '@kirocrew/app-sdk'
import type { ChatMessage, ParsedOptions } from '@kirocrew/app-sdk'

function AgentTurn({ message }: { message: ChatMessage }) {
  // Strip the steer acknowledgement first, then the option marker: the text you render is
  // whatever is left, and the pieces you pulled out become your own affordances.
  const { cleaned, acks } = extractSteeringAcks(message.content ?? '')
  const { text, options, multi }: ParsedOptions = parseOptions(cleaned)

  return (
    <>
      <p>{text}</p>
      {acks.map(a => <SteeredChip key={a} summary={a} />)}
      {options.length > 0 && <MyChoiceButtons options={options} multi={multi} />}
    </>
  )
}
```

To decide whether choices still apply to the *conversation* rather than to one message, use
`deriveFollowUpOptions(messages, isStreaming)`. It walks back to the most recent real assistant turn
and returns none while streaming, after a user reply, or after a queued send — so stale buttons do
not linger:

```tsx
const { followUpOptions } = deriveFollowUpOptions(messages, running)
```

The module imports no React and no dashboard component, so it is also usable from a worker, a test,
or a non-React renderer.

### Using it from a core dashboard page

A page inside `website/src/` imports the same barrel by relative path — there is no second
implementation and no dashboard-only variant:

```tsx
import { parseOptions, stripPartialOptionMarker } from '../../app-sdk/protocol'
```

`stripPartialOptionMarker` exists for the streaming case: mid-stream the text can end with a
half-arrived `[OPTIONS: …` that the full-marker regex cannot match yet, and showing it would let raw
syntax type itself out in front of the user. Apply it to the parsed text while a turn is streaming.

The regex itself is **not** part of the app surface. It carries the global-flag `lastIndex` state, so
handing it out lets an app's `.test()` call make this module's own scan start mid-string and miss the
marker — the exact failure the module exists to prevent. Apps get functions; the pattern stays in-tree.

### Exports

| Export | Kind | Purpose |
|---|---|---|
| `parseOptions(content)` | function | split prose from choices; returns `ParsedOptions` |
| `deriveFollowUpOptions(messages, isStreaming)` | function | the choices that still apply to the conversation |
| `extractSteeringAcks(content)` | function | pull `[STEERING …]` out, returning `{ cleaned, acks }` |
| `stripPartialOptionMarker(text)` | function | hide a half-streamed marker |
| `ParsedOptions` | type | `{ text, options, multi }` |
| `FollowUpDerivation` | type | `{ followUpOptions, followUpSourceKey }`; `followUpSourceKey` names the row the options came from, or `null` when none are on offer |
| `ChatMessage` | type | the message shape `deriveFollowUpOptions` consumes |

The module must stay free of React and of anything under `pages/` or `components/`: a parser that
lives in a component is only available to surfaces that render that component, which is what made a
transcript print raw marker text. `website/src/test/chatProtocolBoundary.test.ts` asserts that, and
also that no other non-test source defines the markers a second time.

## Chat Transcript Rendering

`ChatMessageList` renders a transcript. Which component draws a given row is a **registry** keyed by
the message's `role`, so you add a row type or replace one instead of forking the list.

```jsx
import { ChatMessageList } from '@kirocrew/app-sdk'

<ChatMessageList messages={messages} running={running} />
```

That renders the built-in rows. To change one, pass `renderers`.

### Adding a row the transcript does not draw

Four roles are deliberately undrawn — `thinking`, `system`, `done` and `queued` — because the
dashboard shows them through other affordances. `file` is undrawn too. Claim one and it is yours:

```jsx
const renderers = [{
  id: 'queued-card',
  roles: ['queued'],
  render: (m, ctx) => ctx.row(<div className="queued">{m.content}</div>),
}]

<ChatMessageList messages={messages} running={running} renderers={renderers} />
```

### Limitation: two roles are grouped before your entry is consulted

`thinking` and `permission` (exported as `GROUPED_ROLES`, a frozen array) are assembled into one
collapsible "worked through N steps" group **before** rows are resolved. An entry claiming either is
still consulted, but it renders **inside** that group, and the group keeps its own summary and
approval affordance — so you cannot yet use the registry to replace the built-in approval UI with
your own. Substituting the group itself is not an extension point today — tracked in #2940.

### Replacing a built-in row

Reuse the built-in's `id`:

```jsx
const renderers = [{
  id: 'error',                       // replaces the built-in error row
  roles: ['error'],
  render: (m, ctx) => ctx.row(<MyErrorCard text={m.content} />),
}]
```

Import `defaultMessageRenderers` if you need to read what the built-ins do, and `resolveRenderer` /
`mergeRenderers` if you are composing a registry yourself rather than handing one to
`ChatMessageList`.

### What a renderer is handed

| Field | Purpose |
|---|---|
| `index`, `messages` | position and the whole transcript, for a row that must look ahead |
| `running` | whether the session is producing output |
| `key` | the row's stable React key |
| `wrapper(children, isUser)` | bubble layout; `isUser` right-aligns |
| `row(children, tight)` | full-width layout for cards, pills and banners |
| `onFileOpen` | open a path, when the host supports it |
| `autoDeniedIds` | tool calls a policy or hook blocked |
| `renderTool` | the host's tool row, if it passed one |

Two rules the registry relies on:

- **Shape beats role.** Resolution is first-match, and your entries sit between the two built-ins
  recognised by message *shape* — a stop event and a sub-agent completion, which claim `'*'` and gate
  on a `match` predicate — and the role-keyed ones. This matters because a stop event reaches the
  transcript as role `system`, which is also a role you are invited to claim: were a role claim
  allowed to outrank a `kind` check, claiming `system` would swallow the stop card and pressing Stop
  would draw your row instead. A role claim cannot know about `kind`, so it does not outrank one.
  Replacing a shape-matched row is still possible and stays explicit — reuse its `id`.
- **Returning `null` is different from not claiming a role.** An entry that exists and draws nothing
  says "no row by design"; no entry at all says "nothing handles this". Both look identical on
  screen, so `website/src/test/messageRenderers.test.ts` pins which is which.

### Exports

| Export | Kind | Purpose |
|---|---|---|
| `ChatMessageList` | component | the transcript |
| `defaultMessageRenderers` | value | the built-in registry, in resolution order |
| `mergeRenderers(extra)` | function | shape-matched defaults, then host entries, then the rest |
| `resolveRenderer(message, renderers)` | function | first entry that claims the message |
| `ToolCallPill` | component | the store-free tool row the default registry uses |
| `GROUPED_ROLES` | value | frozen array of the roles grouped before per-row resolution (see the limitation above) |
| `MessageRenderer` | type | `{ id, roles, match?, render }` |
| `MessageRenderContext` | type | what `render` is handed |

The registry takes no store and no router dependency, and reads live state only through the context
it is handed — an app runs outside the dashboard's React root and has no store to select from. A row
that genuinely needs live app state is supplied by the host as an entry.

## Gateway API Surface

The sections below name the Gateway API surface. A name here is an **endpoint
label**, not a guarantee that a client method exists for it: the source-only
`kirocrew-client` Python package covers part of this surface, and
[Python Client](#python-client) marks which part. For anything it does not
implement, call the endpoint directly — the paths are in
[Gateway REST API Endpoints](#gateway-rest-api-endpoints).

The `Returns` column describes the response shape. It is not a TypeScript type:
no TypeScript client ships, so `SlotInfo`, `GatewayStatus`, `SystemInfo` and
their siblings are response-shape names rather than importable types.

When `app_name` is set and no explicit auth is provided, the Python client reads
the app secret from `~/.kiro/crew/apps/{name}/.app_secret`. For a remote Gateway,
call `await client.authenticate()` before the first request; the context manager
does not exchange the secret automatically. The same exchange refreshes a token
after a 401/403 response.

The Gateway names its authentication cookie from the Host header it receives,
falling back to its own listen port. The Python client normally derives that name
from `base_url`. For a port-less URL or a reverse proxy that strips or rewrites
Host, pass `cookie_port=<gateway listen port>` to `KiroCrewClient`; the override
applies to both HTTP requests and WebSocket handshakes created by `create_ws()`.

### Authentication

| Method | Returns | Description |
|--------|---------|-------------|
| `authenticate()` | `boolean` | Exchange the app secret for a token; call explicitly before the first remote request |
| `setToken(token)` | `void` | Conceptual token assignment; the Python client accepts `token=` in its constructor |

### Connection

| Method | Returns | Description |
|--------|---------|-------------|
| `ping()` | `boolean` | Check if Gateway is reachable |
| `getStatus()` | `GatewayStatus` | Gateway health (version, uptime, slots, provider) |
| `getSystemInfo()` | `SystemInfo` | CPU, memory, disk metrics |

### Chat Slots

| Method | Returns | Description |
|--------|---------|-------------|
| `createSlot(name, agent?)` | `SlotInfo` | Create a new chat session |
| `listSlots()` | `SlotInfo[]` | List all active sessions |
| `deleteSlot(slotId)` | `—` (no body) | Remove a session |
| `getSlotHistory(slotId, limit?)` | `{messages, total}` | Get slot message history |
| `sendMessage(slotId, message)` | `—` (no body) | Send a message (validates length, auto-flushes pending context) |

### WebSocket Events

| Method | Returns | Description |
|--------|---------|-------------|
| `connect()` | `void` | Open WebSocket connection |
| `disconnect()` | `void` | Close WebSocket connection |
| `connected` | `boolean` | Current connection state |
| `onChatChunk(slotId, cb)` | `() => void` | Stream response chunks for a slot |
| `onChatDone(slotId, cb)` | `() => void` | Response complete for a slot |
| `onNotification(cb)` | `() => void` | Receive notifications |
| `onToolCall(cb)` | `() => void` | Receive tool call events |
| `onConnectionChange(cb)` | `() => void` | Connection state changes |
| `onRaw(cb)` | `() => void` | All parsed WebSocket events |

All `on*` methods return an unsubscribe function.

The Python client's `on_slot(slot_id, event_type, callback)` dispatches by
`data.slot` for ordinary slot-bound frames. `slot_title` and `session_summary`
instead carry the slot identifier as `data.key`; slot-scoped listeners handle
both wire shapes.

WebSocket event types include `chat_chunk`, `chat_thinking`, `chat_status`,
`chat_message`, `chat_done`, `tool_call`, `tool_result`, `notification`, `slots`,
`slot_title`, `dashboard`, `log`, `refresh`, `approval`, `approval_resolved` and
`subagent_done`. `src/kiro_crew/dashboard/ws_event_scope.py` owns the full set of
emitted types and how each is scoped to an app; read it rather than this list
when you need every type.

An `approval_resolved` frame says a pending approval is no longer waiting. Its
`data` carries:

- `id`: the approval id. The id is the requester's and can recur, so it alone
  does not name one request.
- `approved`: `true` when the request was approved. `false` covers a
  rejection, an expiry and a takeover alike, so read `decision` before
  reporting a rejection.
- `slot`: the owning session, when the approval has one.
- `instance`: present on a coordinator approval. It names the one request that
  ended, so a card raised for a later request under the same id stays live.
- `decision`: absent for an ordinary decision. `"expired"` means the wait timed
  out and the request was denied. `"superseded"` means a new request took over
  the id: nobody answered the old one, and the new request's own `approval`
  frame follows. Do not report a superseded request as rejected.

The app `slots` event (`mc:app:slots`) fires after the dashboard applies each
`slots` or `slot_patch` frame. Its `detail` is `null`: the event says the slot
list changed, not what it now holds. Re-read slots through your own scoped
client when it fires.

### Subagents

| Method | Returns | Description |
|--------|---------|-------------|
| `spawn(task, agent?)` | `string` | Spawn a background subagent |
| `spawnMany(tasks, agents?)` | `string[]` | Spawn multiple subagents in parallel |
| `listSubagents()` | `SubagentInfo[]` | List all subagents |
| `getSubagentStatus(id)` | `SubagentResult` | Get subagent output |

### Cron Jobs

| Method | Returns | Description |
|--------|---------|-------------|
| `addCron(name, options)` | `CronJob` | Create a scheduled job |
| `listCrons()` | `CronJob[]` | List all cron jobs |
| `updateCron(id, options)` | `CronJob` | Update a cron job |
| `removeCron(id)` | `—` (no body) | Delete a cron job |
| `pauseCron(id)` | `—` (no body) | Pause without deleting |
| `resumeCron(id)` | `—` (no body) | Resume a paused job |

**Ownership for app tokens.** `POST /api/crons` from an app token stamps the
job's `created_by` as `app:<name>` from the token. An app token may update,
delete, enable, run, cancel or acknowledge only jobs that carry its own stamp.
Any other id, foreign or missing, answers 403 `owner_only`. A job without an
app stamp is owner-only. An acknowledgement's `ts` must name a cron
notification of that same job.

#### Watching something without paying for a model call (`kiro_crew.irq`)

> **Provisional surface.** `kiro_crew.irq` has two in-tree probes: `gh_pr`
> (`PrWatchProbe`) and `work_ledger` (`WorkLedgerProbe`). Script crons and the
> in-process autonudge loop both drive it. Treat the shapes below as subject
> to change: build on them, but expect `Observation` / `Tick` to gain fields,
> and pin the Kiro Crew version your app was tested against.

An app that needs to keep an eye on an external thing — a deploy, a ticket, a
queue depth — should not schedule an **agent** cron to go look. That spends a
full model turn per check, and on a quiet subject every one of those turns says
"nothing changed".

Schedule a **script** cron instead and build it on `kiro_crew.irq`, the
interrupt controller. The script runs in a subprocess with no model call at
all; a quiet tick is free. Only an unexpected observation raises a wake, and the
wake is delivered into the session that armed the cron as a real agent turn.
Full design: `docs/system-specs/modules/agent-interrupt-controller.md`.

You write the two things that are your domain knowledge — what to poll, and
what counts as an anomaly — and the module owns masking (so one condition wakes
once), coalescing (so several anomalies arrive as one wake), epoch resets (so a
re-triggered subject forgets stale alerts), atomic per-watch state, and a
consecutive-error backstop (so a broken probe says so instead of skipping
quietly forever). Those are the four things a hand-rolled poller gets wrong,
and each failure looks like success.

```python
import json

from kiro_crew.irq import Observation, Probe, Severity, Tick, run


class DeployProbe(Probe):
    def identity(self, ctx):
        """Return (subject_kind, subject_id); raise ValueError to self-remove."""
        self.env = (json.loads(ctx.message or "{}") or {}).get("env") or ""
        if not self.env:
            raise ValueError('needs {"env": "..."}')
        return ("deploy", self.env)

    def observe(self, ctx):
        """One bounded call per tick. Never raise Skip/Report/Done."""
        status = read_deploy_status(self.env)
        if status is None:
            return Tick(fetch_ok=False)          # the kernel owns the backstop
        if status.finished:
            return Tick(epoch=status.id, observations=[
                Observation("done", Severity.TERMINAL, f"{self.env} deployed."),
            ])
        obs = []
        if status.rolled_back:
            # Nothing improves by waiting -> IMMEDIATE bypasses coalescing.
            obs.append(Observation("rollback", Severity.IMMEDIATE,
                                   f"{self.env} rolled back."))
        for stage in status.failed_stages:
            obs.append(Observation(f"stage:{stage}", Severity.WAKE,
                                   f"{self.env}: stage {stage} failed."))
        return Tick(epoch=status.id, observations=obs,
                    pending=status.running_stages)


def watch(ctx):                                   # cron entry point
    run(ctx, DeployProbe())
```

Register it as a **script** cron. Two paths create one:

- From an app hook, call `ctx.cron.add_job(name, message, every_secs=300,
  script="your_probe.py:watch")` (or `add_job_async` on the event loop). A
  relative script path resolves against your app's own bundle.
- From the session that should receive the wake, use the `cron_add` MCP tool
  with its `script` and `timeout` arguments. The script must live under the
  config directory's `crons/`. The cron system captures the calling session at
  creation time, which is why this call must come from that session.

Pass the probe's settings as the cron `message`, for example
`json.dumps({"env": "prod"})`. Do not use `addCron` / `POST /api/crons` for
this: that route reads neither `script` nor `timeout`, so it creates an
**agent** cron that pays a model turn on every tick.

Rules:

- **Never raise `Skip` / `Report` / `Done`.** Return data; the kernel decides.
  It is the only place a verdict is raised.
- A failed observation returns `Tick(fetch_ok=False)`, never an empty `Tick` —
  an empty tick reads as "nothing is wrong".
- Use `Severity.IMMEDIATE` only for what genuinely cannot improve by waiting.
  Using it to mean "important" defeats coalescing.
- Supply an `epoch` when the subject has an identity token. Without one there
  are no resets, so a re-triggered subject inherits the previous run's masks.
- Filter out conditions the operator already knows about (a check red on the
  base branch, a known-degraded dependency) in your own `observe()` — do not
  return them. There is no flag that marks an observation as expected.
- Keep `observe()` to one bounded call. This half must stay fast and cheap.
- `coalesce_secs=0` turns coalescing off — pass it to `run()`, or return it from
  your probe's `tuning()` when it should come from the cron message. Do that when
  you would rather be woken early than woken once: coalescing costs at least one
  cron interval of latency, because a window cannot open and fire within the
  same tick.

### Content Scrubbing (`ctx.scrub`)

Before your app sends content anywhere off the machine — an external document
store, a ticket, a wiki — run it through `ctx.scrub`. It applies the same
credential and exfiltration-URL redaction the gateway applies on its own
boundaries, by reference rather than by copy, so a pattern tightened in a later
release reaches your app with the wheel.

```python
result = ctx.scrub.outbound(body)          # may raise; see below
if result.redacted:
    ctx.logger.info("scrub removed %d credential(s), %d url(s)",
                    result.credentials_removed, result.urls_removed)
publish(result.text)          # only the scrubbed text may leave
```

`outbound(text) -> ScrubResult` carries `text`, `credentials_removed`,
`urls_removed` and `redacted`.

You get **counts, not descriptions**, and there is deliberately no way to learn
which value was removed. That is not an omission to be filled in later: the
gateway's internal exfiltration warning includes the offending domain and the
start of the query string, so handing those through would move a secret out of
your published text and into your logs. Report the fact — "we removed something
before sending" — rather than rewriting the user's content silently.

Use `redacted` rather than comparing against the original. It can be `True` with
both counts at zero: on a host running an edition companion, extra patterns apply
that the base counts do not include. It is never `False` when something was
removed.

**`outbound` can raise, and you must not swallow it.** On a host whose companion
fails to compose, it propagates rather than quietly falling back to weaker
redaction. Abandon the publish when that happens — publishing unredacted is worse
than not publishing.

`outbound` is the only method, on purpose: neither single pass is exposed alone,
because an app that wants half a redaction wants something this seam should not
make easy.

`ctx.scrub` needs **no permission** and is always present: it only removes data, so
there is nothing to withhold and no `None` branch that could become a silent
no-redaction path. **Do not copy these patterns into your app** — a set that drifts
from the gateway's is a control that looks present and is not.

### Audit Events (`ctx.audit`)

When your app acts on the user's behalf against something outside the machine,
record the decision in the same append-only security event log the gateway's own
decisions land in — otherwise "who changed what, and what was refused" is
answerable for the gateway and unanswerable for your half of the same operation.

```python
ctx.audit.record("publish", "success", resources=doc_id)
ctx.audit.record("publish", "denied", resources=doc_id, error="no edit access")
```

`record(operation, outcome, *, resources="", error="")` **never raises** — an audit
sink that is unwritable must not fail the user's publish.

`outcome` is a short verb you choose (`success`, `denied`, `error`, `completed`, …).
It is not checked against a vocabulary — a spelling of your own is kept, because
rewriting it would record something other than what happened. It is redacted and
length-clipped like `resources` and `error`, so a credential that reaches it by
accident is not written; that is a no-op for any real outcome value. This log is
append-only and readable by the dashboard OWNER over `/api/sel/events` — that
endpoint is owner-gated, so no non-owner dashboard user reads it. In-process app
code is NOT isolated from it, though: as the next paragraph says, hook code runs
inside the gateway and can reach the log directly, so treat anything you write
here as readable by a co-resident app. Nothing put in it can be taken back:
don't route free-form remote output through these fields.

There is no `caller=` argument. Attribution is minted from your app name
(`app:<name>`, the same tag `ctx.cron` uses for ownership), so there is no
parameter to pass the wrong value into. It is **cooperative, not unforgeable**: hook
code runs inside the gateway process and can construct another app's SDK or reach
the log directly, so treat `app:<name>` as "which app said this", not as proof.
`operation` is namespaced the same way, so two apps cannot collide on a bare
`"publish"`. No permission gates it: an app cannot obtain anything with it, only
state what it did.

### Durable Jobs (`ctx.job`)

Present when the manifest declares `permissions.jobs`, and `None` otherwise.
Register a runner per job kind in `on_startup`, then start runs by kind:

```python
ctx.job.register("export", run_export, cancellable=True)

run_id = ctx.job.start("export", dedupe_key=doc_id, params={"doc": doc_id})
# or: run_id = await ctx.job.start_async("export", dedupe_key=..., params=...)


def run_export(handle):
    doc = handle.params.get("doc", "")
    ...
```

`start(kind, *, dedupe_key="", params=None)` returns the run id. A second start
with the same kind and `dedupe_key` while a run is in flight adopts that run
instead of starting another. The adopted run keeps the first caller's `params`;
a later caller's `params` are not merged in.

`params` names the work for the runner. It is a flat map of string keys to
string values: at most 16 entries, each key 1 to 64 characters, each value at
most 512 characters. A map that breaks these bounds is refused with an error,
not trimmed. A value that looks like a credential is refused too, because the
run record is durable; `params` is not a way to pass a runner a secret.

The runner reads `handle.params`, which returns a fresh copy on each read.
Editing that copy changes nothing on the record.

`params` and `dedupe_key` are never served in the run view the `_jobs/*` routes
return. Only the SDK call takes `params`: the HTTP start route
(`POST /api/apps/{app}/_jobs/{kind}/start`) accepts a `dedupe_key` and no
parameters.

### Gateway Application (`ctx.http_app`)

The gateway's own aiohttp `Application`, for background work that must be anchored
on it — a poller that has to read the same dashboard state your request handlers
read, and stash its running service where those handlers look it up.

```python
async def on_startup(ctx):
    if ctx.http_app is None:
        ctx.health.mark_degraded("poller not started: no gateway application on this host")
        return
    await start_my_poller(ctx.http_app)


async def on_shutdown(ctx):
    if ctx.http_app is None:
        return
    await stop_my_poller(ctx.http_app)
```

Present **only if your manifest declares a `routes` hook**, and `None` otherwise.
That gate is not a permission you can ask for: an app with routes is dispatched the
real `web.Request`, so `request.app` is already this exact object and the field adds
no reach. An app with lifecycle hooks and no routes has no request path either, so
handing it the Application would be a genuinely new grant.

Read it with `getattr(ctx, "http_app", None)` if your app must also run on a gateway
older than this field, and **report the gap** — `ctx.health.mark_degraded` with the
user-visible consequence — rather than returning quietly. Background work that
silently never starts is indistinguishable from having nothing to do.

Your `on_startup` and `on_shutdown` contexts are built by different code paths and
are guaranteed to agree about this field, so work you start with it can always be
stopped with it. That holds across a version bump too: teardown reuses the answer
recorded when the app was enabled, so dropping your `routes` hook in a later release
does not strand the work an earlier one started — your `on_shutdown` still receives
the Application it was given. The same rule runs the other way, so adding a `routes`
hook does not hand the object to a teardown whose startup never held it. Declare
`on_shutdown` whenever you declare `on_startup`: anything you spawn outlives the
startup call, and teardown is the only thing that stops it.

### Lessons

| Method | Returns | Description |
|--------|---------|-------------|
| `addLesson(rule, category, scope?)` | `—` (no body) | Save a learned rule |
| `listLessons()` | `Lesson[]` | List all lessons |
| `removeLesson(query)` | `—` (no body) | Remove matching lessons |

### Notifications

| Method | Returns | Description |
|--------|---------|-------------|
| `sendNotification(text, options?)` | `—` (no body) | Send via Slack or dashboard |
| `listNotifications()` | `{notifications}` | List notifications |
| `ackNotifications()` | `—` (no body) | Acknowledge all notifications |

### Approvals

| Method | Returns | Description |
|--------|---------|-------------|
| `approveAction(slotId, taskId, pattern?)` | `—` (no body) | Approve a pending tool action; command/base trust requires the pending card pattern |
| `rejectAction(slotId, taskId)` | `—` (no body) | Reject a pending tool action |
| `resolveApproval(approvalId, action, slotId?, pattern?)` | `—` (no body) | Resolve an approval by ID; `trust_command` and `trust_base` require `pattern` |
| `getApprovalMode()` | `'auto'` \| `'interactive'` | Read the config setting `agent.approval_mode` from `GET /api/config/kirocrew` |
| `setApprovalMode(mode)` | `—` (no body) | Set the chat trust mode (`normal`, `trust_reads`, `trust`, `yolo`) through `POST /api/chat/mode`; a different setting from `agent.approval_mode` |

**Ownership for app callers** (a dashboard caller is unrestricted). The rule follows the request's app claim: an app token, and also an agent running in one of your app's sessions, whose internal calls carry your app's identity.
- `POST /api/approvals/{id}/{action}` needs `permissions.sessionApproval`, exactly like `POST /api/chat/slots/{slot}/approve`: without it both answer 403 `session_approval_not_granted` to an app-token caller, even for your own slots. An agent running in your app's sessions (its cron job or subagent, calling over the internal secret) is exempt from that grant on both routes alike, and is still limited to the slots your app owns. With the grant, the id resolves a request on a slot you own or on a local user session. Request ids can recur across sessions, so an id pending on more than one session you may control is refused; name the session with the slot route instead (`resolveApproval(id, action, slotId)`). A background approval (cron, autonudge, subagent, task runner) is never resolvable by an app.
- `GET /api/approvals` lists only what the app could resolve. The list carries background approvals only, so for an app it is always empty. Read a slot's pending requests from its history instead.
- `GET /api/sessions`, `GET /api/sessions/search`, `GET /api/sessions/{key}`, `DELETE /api/sessions/{key}` and `POST /api/sessions/summarize` reach only transcripts that record the calling app as their owner. `total` counts only those, search ranks only those, and summarize skips any other key as if it were missing (so `list_sessions` with `summarize` from your app's agent returns summaries for your transcripts only). A delete is also refused when a live slot holding the transcript belongs to someone else.
- `DELETE /api/sessions` (bulk clear) and `GET /api/sessions/clearable/count` act on every closed transcript at once, so an app is always refused them.
- A new slot can only be opened on a fresh name or on a transcript your app already owns. `POST /api/chat/slots` with a `name`, `POST /api/chat` that would create its slot, and `POST /api/chat/slots/{slot}/resume` answer 404 for a transcript that belongs to someone else.
- Apart from the missing-grant 403, every refusal returns the same body as a missing target, `{"error": "not found", "code": "slot_not_found"}`. The reason is recorded in the security-event log, and so is every access the ownership rule allows.
- `GET /api/sessions/{key}` can answer 503 `session_busy` when the transcript is being written at that moment. Retry it.
- Not narrowed yet: `/api/sessions/{id}/agents*`, `POST /api/sessions/restart`, and `/api/sessions/usage`, `/health` and `/memory`. Narrowing them to the app's own sessions is planned, so do not build on reaching other sessions there.

### Models

| Method | Returns | Description |
|--------|---------|-------------|
| `listModels()` | `ModelInfo[]` | List available LLM models |
| `setSlotModel(slotId, model)` | `—` (no body) | Set model for a slot |

### MCP Servers

| Method | Returns | Description |
|--------|---------|-------------|
| `listMcpServers()` | `McpServerInfo[]` | List registered MCP servers |
| `registerMcpServer(def)` | `—` (no body) | Register an MCP server (requires name + command) |
| `removeMcpServer(name)` | `—` (no body) | Remove an MCP server |

Apps declare their agents, skills and `mcpServers` in `app.json`, and
`kirocrew app install` installs them. For an ad-hoc MCP server, use
`registerMcpServer`.

### Agent Runtime

| Method | Returns | Description |
|--------|---------|-------------|
| `dispatchAgent(agent, prompt)` | `TaskResult` | Run agent synchronously |
| `dispatchAgentAsync(agent, prompt)` | `string` | Run agent in background |
| `getTaskResult(taskId)` | `TaskResult` | Poll task status |

### Gateway Config

| Method | Returns | Description |
|--------|---------|-------------|
| `getGatewayConfig(key)` | a JSON object | Read gateway config section |
| `setGatewayConfig(key, value)` | `—` (no body) | Write gateway config section |

### App Storage

| Method | Returns | Description |
|--------|---------|-------------|
| `getAppDataDir()` | `string` | App-scoped data directory path |
| `getAppConfig()` | a JSON object | Read app config via REST |
| `setAppConfig(config)` | `—` (no body) | Write app config via REST |

### Memory

| Method | Returns | Description |
|--------|---------|-------------|
| `memorySearch(query, topK?)` | `MemoryResult[]` | Semantic memory search |

### Context Injection

Silent background context for LLM — content appears in the next user-initiated turn without triggering a response or showing a visible message.

| Method | Returns | Description |
|--------|---------|-------------|
| `injectContext(slotId, content, options?)` | `—` (no body) | Inject context (null slotId = buffer locally) |
| `flushPendingContext(slotId)` | `number` | Flush buffered entries to a slot; returns the count the full queue refused and dropped. A `context_not_queued` entry is dropped (not re-buffered) so it never blocks later sends; a transient failure is re-buffered and raised |
| `setDefaultSlot(slotId)` | `void` | Auto-flush pending context on sendMessage |
| `pendingContextCount` | `number` | Number of buffered context entries |

Options: `{ source?: string, ephemeral?: boolean, maxAge?: number }`

**Constraints** (400 on violation):
- `source`: ≤64 chars, no control characters or newlines; whitespace-trimmed (a padded label and its bare form share one per-source cap bucket)
- `maxAge`: must be a finite positive number (rejects boolean, NaN, Infinity, ≤0); omit or pass null for no expiry
- `content`: must be a non-empty string, ≤40,000 chars

**Ownership** (404 on refusal; applies to app callers — a dashboard caller is unrestricted):
- An app may only target a slot it owns, and a slot carrying no app scope is refused as well.
- Owning the slot is not sufficient: an app is refused when the slot's session is linked elsewhere — a cron result or workflow injection holding that binding — because both writes land in the linked session, so slot ownership alone would otherwise reach a conversation the app has no claim on.
- Every refusal returns the same body as a genuinely missing slot, so no response an unauthorized caller can reach distinguishes "not yours" from "does not exist". The specific reason is recorded in the security-event log instead.

**Full queue (429 `context_not_queued`).** A slot holds at most 50 pending context entries (held notes' context halves count against that ceiling). When a `/context` POST would exceed the ceiling it is refused with `429` and body `{ error, code: "context_not_queued" }`; expired entries are reclaimed first, so a live entry the caller already holds a 200 for is never evicted to make room. The refusal is **not transient in the Retry-After sense**: only a user-initiated turn drains the queue, so a tight retry loop cannot clear it. The queue is drained on the next turn; retry the inject after a turn, or let a turn consume the backlog. (The per-source cap is separate: it answers `429 context_not_queued`'s sibling `capacity_reached` and only bounds one `source` bucket.) The shipped Python client surfaces this as the non-retried `CONTEXT_NOT_QUEUED` error code rather than `RATE_LIMITED`, and `flush_pending_context` drops a refused entry instead of blocking later sends — see the Python client section below.

### Notes

`POST /api/chat/slots/{slot}/note` drops a short declarative line into a chat that is both visible in the transcript immediately and known to the agent on the user's next message — without firing an LLM turn. Context injection alone is silent; a transcript append alone is invisible to the model, because a live provider forwards only the new user message. The note endpoint does both writes against one slot.

Body: `{ content, source?, maxAge?, ephemeral? }`. A note always does both writes -- there is no visible-only or context-only mode. The visible line is appended as `role: "inject"` with `cls: "reconcile-note"`, and its content is redacted (credentials, exfiltration URLs) before it reaches the transcript. The bubble's author pill comes from the authenticated app identity, never from the body's `source`. A dashboard user's note has no pill, and neither does a note from an app whose name is over 64 characters or contains a control character. The body's `source` is only the label of the context frame. `maxAge` defaults to 24h for the context half when the key is omitted, so a note nobody follows up on expires instead of attaching to an unrelated message later. An explicit null means no expiry, the same as it does on `/context` — the two endpoints share the field and do not give it opposite meanings. The same `source`/`maxAge`/`content` constraints above apply.

Returns `{ ok, appended, visibleDeferred, deliveryConditional, contextSkipped, pending }`. `contextSkipped` is true whenever the context half is refused but the visible line is still written: either the source's per-source context cap is full, **or** the whole 50-entry pending-context queue has no seat. The request is **not** rejected in either case, because the caps protect the context queue rather than the transcript. The full-queue refusal is checked at admission on both arms — an immediate note (`append_pending_context` returns false) and a note held during a running turn (`has_pending_context_seat()` is false) — so a held note never acknowledges a context half the turn-end flush would then drop. If a turn is already running the note is held until that turn ends -- `appended` is false and `visibleDeferred` is true -- so that it lands on the next turn rather than the one it was written during. Ordering is preserved, and `deliveryConditional` is true whenever a note is held -- because a hold is delivered only if the slot still routes to the SAME session when the turn ends. An unbound slot can acquire a foreign binding while the note waits (a cron result or workflow injection claims an empty `linked_session_key` with no running gate), and both the transcript path and the next turn's session resolve that binding at flush time rather than at the POST. When that happens BOTH halves of the note are dropped rather than retargeted, because writing them would surface content authorized for one conversation inside another; the drop is recorded in the security-event log. So a 200 with `visibleDeferred: true` promises ordering against the running turn, not that the note will certainly be written. `pending` counts held entries as well as queued ones.

**A 200 for a held note is a durable acknowledgement — for a slot that has a durable identity.** The hold is persisted verbatim (both halves, the silent context included) into the slot's own session metadata *before* the 200 is returned, replayed into the hold by both slot-restore paths after a gateway restart, and retired by the save that commits the delivered rows -- so a note accepted with `visibleDeferred: true` survives a restart and is delivered, unaltered, on the first turn after it. Two edges keep the original gateway-lifetime meaning instead: a memory-only deployment (no conversation log at all), and a slot that has never been persisted (no metadata line to attach the hold to -- such a tab does not itself survive a restart, so there is no restored slot the note could outlive). Do **not** re-post a held note after a restart; the restored hold delivers it, and a re-post would put the same line in the transcript twice. Three boundary refusals protect that promise: a note posted during a running turn is capped at 4,000 characters (`413`, code `deferred_note_too_large` -- shorten it or wait for the turn to end), a slot whose durable hold is full answers `429 deferred_notes_full` until its rows are saved, and a slot that is rebound to another session while the hold is persisting answers the endpoint's uniform `404` -- the note was neither delivered nor made durable (a note the turn-end flush drops at that same rebind seam takes this `404` too; the 200 stands only when the note observably exists in a delivered row or the durable hold). The one retry-the-same-request signal is a `503` with code `deferred_note_persist_failed`, which means the durable write itself failed and the note was **not** accepted. The queued context of an *immediate* (non-held) note still behaves exactly as `/context`'s queue always has -- in memory, for this gateway lifetime. Note the retention consequence of durability: a HELD note's context half -- the trusted-caller channel, which is deliberately not redacted -- lives on disk in the session metadata until delivery or retirement, while an immediate note's context lives only in memory.

### Proxy Authentication (Server-side)

Verify that an incoming request was signed by the Kiro Crew Gateway reverse proxy.
Use these main-package helpers in Python app backends:

| Function | Returns | Description |
|----------|---------|-------------|
| `raw_request_target(request)` | `str` | Preserve the raw percent-encoded path and query that the Gateway signed |
| `proxy_secret()` | `str` | Read the injected `KIROCREW_PROXY_SECRET`, or an empty string |
| `verify_proxy_request(header, *, method, target, body, secret=None, now=None)` | `bool` | Verify the body-bound HMAC and fixed ±60-second freshness window; fail closed on malformed input |

---

## Python Client

Standalone async client using `aiohttp`, carried in this repository under
`packages/kirocrew-client-py/`. It is not published to PyPI or included in the
main wheel. Install it from a source checkout; it covers part of the Gateway API
surface documented above.

```bash
python -m pip install -e /path/to/KiroCrew/packages/kirocrew-client-py
```

```python
from kirocrew_client import KiroCrewClient

async with KiroCrewClient(app_name="my-app") as mc:
    ok = await mc.ping()
    slots = await mc.list_slots()
```

### Constructor

```python
KiroCrewClient(
    *,                        # every argument is keyword-only
    base_url="",              # default: http://localhost:{KIROCREW_PORT or 5476}
    token="",                 # optional for localhost
    app_name="",              # app-scoped storage and secret lookup
    timeout=30,               # request timeout seconds
    max_retries=3,            # retry count
    retry_base_delay=1.0,     # base delay for backoff
    message_length_limit=40000,
    on_auth_expired=None,     # async callback returning new token
    cookie_port=None,         # gateway listen port for the auth cookie name
)
```

With `app_name` set and no `token` or `on_auth_expired`, the client reads the
app secret from `$KIROCREW_HOME/apps/{name}/.app_secret` (`KIROCREW_HOME`
defaults to `~/.kiro/crew`).

For a remote Gateway, pass `token=...` or call `await client.authenticate()`
after entering the context. Local loopback requests need no token. Setting
`app_name` alone only locates the app secret; it does not authenticate during
`__aenter__`.

Token authentication uses the Gateway's port-scoped `mc_token_<port>` cookie. If
`base_url` omits the port, the client uses the scheme default (443 for HTTPS/WSS,
80 for HTTP/WS). A Gateway receiving a Host header without a port instead keys
the cookie to its own listen port, so a port-less URL matches only when the
Gateway listens on the scheme-default port. Pass an explicit port when it listens
elsewhere, including behind a reverse proxy that removes the port from Host.

Core requests retry 429 responses for every HTTP method, with one carve-out: a
`429` carrying code `context_not_queued` (a full pending-context queue) is **not**
retried — only a turn drains that queue, so backoff cannot clear it — and surfaces
as the `CONTEXT_NOT_QUEUED` error code. They retry 5xx responses and
transport failures only for `GET`, `PUT`, and `DELETE`; `POST` and `PATCH` are not replayed
when the server may already have applied them. A 401/403 refusal may still refresh authentication
and replay once because the Gateway rejected the request before applying it.

### Method Reference

The left column is the endpoint label used in the sections above; the right
column is the shipped Python method, in `snake_case` per Python convention.

Rows marked *not implemented* are Gateway endpoints the shipped Python client
does not wrap yet. Call those endpoints directly with `aiohttp` (or any HTTP
client) using the paths in
[Gateway REST API Endpoints](#gateway-rest-api-endpoints).

`create_ws()` returns a `WsClient` bound to `/api/ws` with this client's auth
cookie. Its listeners (`on(type, cb)`, `on_slot(slot, type, cb)`, `on_raw`, and
`on_connection_change`) each return an unsubscribe function;
`connect()` starts a background reconnect loop with exponential backoff and
`disconnect()` stops it for good. When the client can refresh its token, it
does so before each reconnect.

| API surface | Python |
|-----------|--------|
| `ping()` | `ping()` |
| `getStatus()` | `get_status()` |
| `getSystemInfo()` | `get_system_info()` |
| `createSlot(name, agent?)` | `create_slot(name, agent="")` |
| `listSlots()` | `list_slots()` |
| `deleteSlot(id)` | `delete_slot(id)` |
| `getSlotHistory(id, limit?)` | `get_slot_history(id, limit=50)` |
| `sendMessage(id, msg)` | `send_message(id, msg)` |
| `streamChat(id, msg)` | `stream_chat(id, msg)` → async iterator of chunk dicts; an SSE response ends only at `[DONE]` (transport failure or earlier clean close raises `NETWORK_ERROR` without retry), while a successful JSON queue, steer, or orchestrator-control receipt is yielded once and ends the iterator |
| `stopSlot(id, force?)` | `stop_slot(id, force=False)` |
| `editResend(id, content, opts)` | `edit_resend(id, content, *, index=None, ts=None)` |
| `spawn(task, agent?)` | `spawn(task, agent="")` |
| `spawnMany(tasks, agents?)` | `spawn_many(tasks, agents=None)` |
| `listSubagents()` | `list_subagents()` (reads `agents`) |
| `getSubagentStatus(id)` | `get_subagent_status(id)` |
| `addCron(name, opts)` | `add_cron(name, **opts)` |
| `listCrons()` | `list_crons()` (reads `jobs`) |
| `updateCron(id, opts)` | `update_cron(id, **opts)` (`PATCH`) |
| `removeCron(id)` | `remove_cron(id)` |
| `pauseCron(id)` | `pause_cron(id)` |
| `resumeCron(id)` | `resume_cron(id)` |
| `addLesson(rule, cat, scope?)` | `add_lesson(rule, cat, scope="")` |
| `listLessons()` | `list_lessons()` (reads `lessons`) |
| `removeLesson(query)` | `remove_lesson(query)` |
| `sendNotification(text, opts?)` | `send_notification(text, **opts)` |
| `listNotifications()` | `list_notifications()` → `{notifications, unread}` |
| `ackNotifications()` | `ack_notification(ts)` / `ack_all_notifications()` |
| `approveAction(slot, task, pattern?)` | `resolve_approval(request_id, "approved", slot_id=slot, pattern=pattern)`; command/base trust passes the pending card pattern |
| `rejectAction(slot, task)` | `resolve_approval(request_id, "rejected", slot_id=slot)` |
| `resolveApproval(id, action, slot?, pattern?)` | `resolve_approval(id, action="approve", slot_id="", pattern="")`; `approve`/`approved` and `reject`/`rejected` are aliases; without a slot, accepted actions are `approve`, `reject`, `reject_once`; with a slot, accepted actions are `approved`, `rejected`, `trust`, `trust_reads`, `trust_command`, `trust_base`, `yolo`; command/base trust requires `pattern` |
| `listApprovals()` | `list_approvals()`; pending background approvals only (cron, autonudge, subagent, task runner), never a slot's own tool prompts; empty for an app token |
| `getApprovalMode()` | `get_gateway_config("kirocrew")`, then read `agent.approval_mode` |
| `setApprovalMode(mode)` | `set_approval_mode(mode, slot_id="")`; accepted modes are `normal`, `trust_reads`, `trust`, `yolo`; `normal`, `trust_reads`, and `trust` may target one slot, while `yolo` is process-global and rejects `slot_id` |
| `listModels()` | `list_models()` |
| `setSlotModel(slot, model)` | `set_slot_model(slot, model)` |
| `getGatewayConfig(key)` | `get_gateway_config(key)`; `key` is one of `GATEWAY_CONFIG_KEYS` (`kirocrew`, `stt`, `theme`, `default-agent`) |
| `setGatewayConfig(key, val)` | `set_gateway_config(key, val)` (`PUT`, same keys) |
| `listMcpServers()` | `list_mcp_servers()` (`GET /api/mcp`) |
| `registerMcpServer(def)` | `register_mcp_server(name, cmd, args?, env?)` |
| `removeMcpServer(name)` | `remove_mcp_server(name)` |
| `dispatchAgent(agent, prompt)` | `dispatch_agent(agent, prompt)` |
| `dispatchAgentAsync(agent, prompt)` | `dispatch_agent_async(agent, prompt)` |
| `getTaskResult(id)` | `get_task_result(id)` |
| `getAppDataDir()` | `get_app_data_dir()` → `Path` |
| `getAppConfig()` | `get_app_config()` |
| `setAppConfig(cfg)` | `set_app_config(cfg)` |
| `memorySearch(q, topK?)` | `memory_search(q, top_k=8)` (sent as `limit`, capped at 50) |
| `transcribe(audio)` | `transcribe(audio_bytes, *, filename=, content_type=)` → text; transport failures raise `NETWORK_ERROR` without retry |
| `connect()` / `on*` | `create_ws()` → `WsClient` (see above) |
| `injectContext(slot, content, opts?)` | `inject_context(slot, content, *, source?, ephemeral?, max_age?)` |
| `flushPendingContext(slot)` | `flush_pending_context(slot)` |
| `setDefaultSlot(slot)` | `set_default_slot(slot)` |

The standalone package exports `KiroCrewClient`, `KiroCrewError`, `ErrorCode`,
`WsClient`, `WsEvent` and `GATEWAY_CONFIG_KEYS`. It does not export
`AppManifest`, `AppLifecycle`, `GatewayManager` or proxy-auth helpers. Validate
manifests through the main package's install path, manage the Gateway with the `kirocrew` CLI, and use
`kiro_crew.apps.proxy_auth` only from a backend that can import the main package.

---

## Error Handling

All `kirocrew-client` errors are `KiroCrewError` instances with `code`,
`status` and `body` attributes. The message is `str(e)`.

| Code | Trigger | Retried? |
|------|---------|----------|
| `AUTH_REQUIRED` | Remote connection without token | No |
| `AUTH_EXPIRED` | 401/403 response | No (calls on_auth_expired if set) |
| `VALIDATION_ERROR` | Invalid input | No |
| `NOT_FOUND` | 404 response | No |
| `RATE_LIMITED` | 429 response | Yes (Retry-After or backoff) |
| `CONTEXT_NOT_QUEUED` | 429 with code `context_not_queued` (pending-context queue full) | No (only a turn drains the queue) |
| `SERVER_ERROR` | 5xx response | `GET`/`PUT`/`DELETE`: yes; `POST`/`PATCH`: no |
| `NETWORK_ERROR` | Timeout, connection failure, or truncated chat stream | Core `GET`/`PUT`/`DELETE`: yes; core `POST`/`PATCH`, `stream_chat`, and `transcribe`: no |
| `WS_DISCONNECTED` | Reserved enum value; `WsClient` reconnects on its own and does not raise it | No |

```python
from kirocrew_client import KiroCrewError

try:
    await mc.send_message("slot-1", "hello")
except KiroCrewError as e:
    print(e.code, str(e), e.status)
```

---

## Gateway REST API Endpoints

The `useAppApi()` hook can call declared paths, while the source-only Python
client wraps the subset named below. These API routes require the appropriate
dashboard, app, or internal credential; a bare `curl` request is not authenticated.

### Core endpoints used by the Python client

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Gateway status and connectivity check |
| GET | `/api/system` | System metrics |
| GET/POST | `/api/chat/slots` | List or create chat slots |
| GET/DELETE | `/api/chat/slots/{slot}` | Read slot detail/history or delete a slot |
| POST | `/api/chat` | Send a chat turn |
| GET/POST | `/api/spawn` | List or start subagents |
| GET | `/api/spawn/{agent_id}` | Read subagent status |
| GET/POST | `/api/crons` | List or create cron jobs |
| PATCH/DELETE | `/api/crons/{job_id}` | Update or delete a cron job |
| POST | `/api/crons/{job_id}/enable` | Pause or resume a cron job |
| POST | `/api/crons/{job_id}/run` | Run a cron job now |
| POST | `/api/crons/{job_id}/cancel` | Cancel a running cron job |
| POST | `/api/crons/{job_id}/ack` | Acknowledge a cron job's notification |
| GET/POST/DELETE | `/api/lessons` | List, add, or remove lessons |
| POST | `/api/send-message` | Send a notification/message |
| GET | `/api/mcp` | List MCP server configuration |
| PUT/DELETE | `/api/mcp/servers/{name}` | Register or remove an MCP server |
| GET | `/api/memory/episodic/search` | Search memory |
| POST | `/api/chat/slots/{slot}/context` | Inject silent context |

### App Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/apps` | List all installed apps |
| GET | `/api/apps/registry` | List available apps from registry |
| GET | `/api/apps/blob?repo=&path=&ref=` | Proxy images from a registry app's git repo |
| POST | `/api/apps/install` | Install from local path (owner only) |
| POST | `/api/apps/register` | Register a self-managed app (owner only) |
| POST | `/api/apps/registry/install` | Install from registry (owner only) |
| POST | `/api/apps/registry/install-stream` | Install from registry with an SSE progress stream (owner only) |
| GET | `/api/apps/{name}` | Get app details |
| GET | `/api/apps/{name}/manifest` | Get app manifest |
| GET/PUT | `/api/apps/{name}/config` | Read/write app config (PUT from a dashboard subject: owner only) |
| POST | `/api/apps/{name}/update` | Update installed app (owner only) |
| POST | `/api/apps/{name}/uninstall` | Uninstall app (owner only) |
| POST | `/api/apps/{name}/enable` | Enable app (owner only) |
| POST | `/api/apps/{name}/disable` | Disable app (owner only) |
| POST | `/api/apps/{name}/dev` | Toggle dev mode (live reload) — body `{"enabled": bool}` |
| POST | `/api/apps/{name}/open` | Launch app via openCommand (owner only) |
| GET | `/apps/{name}/ui/{path}` | Serve app UI bundle files |
| * | `/apps/{name}/api/{path}` | Reverse proxy to app backend (HMAC-signed). An ordinary response is bounded at 30s total. A `text/event-stream` response has no total, but must emit an event or a `:` comment at least every 60s, or it is cut |

"Owner only" routes answer 403 `owner_only` to a dashboard subject that is not
the owner, and to every app token, including an app calling its own
`/api/apps/<self>/...` path. A config `PUT` from an app token is scoped by the
token's own app permissions instead.

### Reverse Proxy Authentication

The gateway signs each proxied request with `X-KiroCrew-Proxy: <timestamp>:<hmac-sha256>`. The
HMAC is computed over the message `timestamp:method:/api/path[?query]:sha256(body)` using the
app secret as the key, where `sha256(body)` is the hex SHA-256 digest of the raw request body
(an empty body hashes the empty byte string, `e3b0c442...`). Binding the body hash means a
tampered body invalidates the signature. Backends verify with a constant-time comparison and
reject requests whose timestamp is not within ±60s of now.

A Python app backend whose environment can import `kiro_crew` (the built-in app backends run as child processes and still import it) verifies this with the gateway's own helper:

```python
from kiro_crew.apps.proxy_auth import raw_request_target, verify_proxy_request

body = await request.read()
if not verify_proxy_request(
    request.headers.get('X-KiroCrew-Proxy', ''),
    method=request.method,
    target=raw_request_target(request),
    body=body,
):
    return Response(status=401)
```

Every argument after the header value is keyword-only. Pass the target through
`raw_request_target`: the gateway signs the request-target exactly as it went on
the wire, and rebuilding it from a decoded path diverges from the signed bytes as
soon as a query parameter carries a space or a non-ASCII character.

A backend that cannot import `kiro_crew` (a different language, or a Python
environment without the package) computes the HMAC itself, exactly as the Node.js
paragraph below describes.

Node.js app backends can verify the signature directly: compute
`HMAC-SHA256(timestamp:method:/api/path[?query]:sha256(body), app_secret)` and compare against
the value in the `X-KiroCrew-Proxy` header (constant-time), rejecting stale timestamps.

> **Body-bound signature:** every verifier must bind `sha256(body)` while keeping the
> constant-time compare and the ±60s freshness window. A gateway that signs body-bound
> HMACs fails verification against any verifier that omits the body hash, so a
> backend that implements the HMAC itself has to be updated in lockstep with the gateway.

### Backend Environment Variables

The gateway spawns each `backend.entryPoint` app as a sandboxed child and injects a fixed,
generic set of environment variables. No app-specific variables are ever injected.

| Variable | Always set | Meaning |
|---|---|---|
| `PORT` | yes | The loopback port your backend must bind (`127.0.0.1:$PORT`). |
| `KIROCREW_APP_NAME` | yes | This app's installed name. |
| `KIROCREW_HOME` | yes | The gateway's resolved data home, so the backend reads the same app tree. |
| `KIROCREW_GATEWAY_ORIGIN` | only with bound-port evidence | The gateway's own origin, `http://<bound host>:<bound port>`, for calling back to the gateway (for example `POST /api/notifications/push`). It is set ONLY from the address the gateway ACTUALLY bound: its exported `KIROCREW_BOUND_PORT` (required to be numeric and in `1..65535`), with the host `127.0.0.1` for loopback and wildcard binds and `[::1]` for an IPv6-loopback bind. A gateway bound to one specific interface omits the variable entirely: backend callbacks carry no `Origin` header, which the gateway's CSRF barrier trusts only from a loopback peer, so a specific-interface origin would have every mutating callback refused. It is never your app's `PORT`, an inherited `KIROCREW_PORT`, a config value, a default, or a request-derived value, so a child can never be pointed at a sibling gateway. Without loopback bound-address evidence the variable is omitted entirely (see below). |
| `KIROCREW_PROXY_SECRET` | only if a secret exists | The per-app secret used to verify the `X-KiroCrew-Proxy` header (see above). |
| `KIROCREW_SPAWNED` | yes | `1`: a Kiro Crew gateway spawned this process. |
| `KIROCREW_SPAWN_INSTANCE` | yes | An id unique to this spawn. |
| `KIROCREW_GATEWAY_ORIGIN_PROOF` | only if a secret exists and the origin is set | `HMAC-SHA256(app_secret, KIROCREW_GATEWAY_ORIGIN)`, hex. Recompute it with your secret to confirm the injected origin was minted by this gateway, rather than an inherited or spoofed env value. Omitted whenever the origin is omitted (nothing to prove) or no secret exists (nothing to key it with). |

Security and lifecycle:

- When a backend's leader exits and leaves its process group behind, the gateway
  reaps that group by signalling only the members that carry both
  `KIROCREW_SPAWNED` and this spawn's `KIROCREW_SPAWN_INSTANCE`. A child process
  your backend starts must inherit both and stay in the backend's session group,
  or it is not reaped.
- The per-app secret lives on disk at `<KIROCREW_HOME>/apps/<name>/.app_secret`, written
  owner-only `0600` (owner-only DACL on Windows) by the gateway.
- If no `.app_secret` exists, the gateway injects neither the secret nor anything derived from
  it (including the proof); a secret-less backend is otherwise unchanged.
- `KIROCREW_GATEWAY_ORIGIN` is fail-closed: it is present only when the gateway has real
  evidence of the port it bound. A gateway that has not exported a valid `KIROCREW_BOUND_PORT`
  hands the backend no origin, so a backend that needs a callback base stays dormant rather
  than trusting a guessed address.
- The origin and its proof are recomputed on every spawn, so a gateway restarted on a
  different bound port hands the backend the current origin. That freshness guarantee is
  scoped to SPAWNED instances: an externally managed backend the gateway ADOPTS (already
  healthy on its port) keeps the environment of the generation that started it, so its
  origin can be stale. A backend that keeps a long-lived callback base should treat
  persistent push failures as a stale origin and restart to pick up the current one.
- The proof is a SPAWN-TIME attestation, not a liveness or freshness signal: it says the
  origin value in your environment was planted by a gateway holding your `.app_secret`
  when your process started. Because the secret persists across gateway generations, a
  stale origin (the adopted case above) still carries a valid proof — verifying the proof
  tells you the origin was not planted by a secret-less spawner, and nothing about whether
  that gateway is still the one serving. Do not use it as origin-trust for a long-lived
  process; use the push-failure/restart guidance above for that.

Using the origin for notifications:

An entryPoint backend that declares `notifications.channels` in `app.json` pushes with
`POST {KIROCREW_GATEWAY_ORIGIN}/api/notifications/push`, authenticating with its app secret
(see App Notifications). Verify `KIROCREW_GATEWAY_ORIGIN_PROOF` before you trust the origin as
your callback base. If `KIROCREW_GATEWAY_ORIGIN` is unset the gateway did not publish a
bound-port origin, so the backend has no callback base and should not push.

## App Dev Mode (live reload)

Dev mode speeds up app-UI iteration: no manual copy-and-hard-refresh loop. When
an installed app is in dev mode the gateway serves its UI files with
`Cache-Control: no-store` and watches the app's `ui/` directory; on any file
change it broadcasts an `app_reload` WebSocket event and the dashboard reloads
the app so edits appear immediately.

The recommended setup symlinks the **whole `ui/` directory** —
`~/.kiro/crew/apps/<name>/ui` → your source tree — so the watcher sees edits at
the real files. Link the directory, **never individual files inside it**: the
UI route opens the final path component with `O_NOFOLLOW` (a swap-resistant
open), so a per-file symlink like `ln -s ~/src/app/dist/index.mjs ui/index.mjs`
answers `404` — indistinguishable from "not built yet". The directory link
works because the route resolves the ui root *through* the link before
validating files against it.

**Contract surface:**

- **`installed.json` field — `dev: bool`** (default `false`): persisted per-app
  flag. Tolerant on read (absent ⇒ `false`); reversible; no migration needed.
  Builtin apps cannot enter dev mode. This field controls **watching and
  `no-store` serving only** — it is app-writable metadata and never authorizes
  anything by itself (see the grant record below).
- **Endpoint — `POST /api/apps/{name}/dev`**, body `{"enabled": <bool>}`.
  Returns `{"name": <name>, "dev": <bool>}`. `400` for a non-boolean body,
  a builtin app, an unsafe app name, or a refused grant (see below); `404` if
  the app is not installed. Behind the standard gateway auth; emits an
  `app_dev_mode` SEL audit event. The endpoint deliberately has no field to
  confirm an out-of-install root — that confirmation is CLI-only (below).
- **WebSocket event — `app_reload`**, payload `{"app": <name>, "ts": <float>}`.
  Re-dispatched to the frontend as the `mc:app-reload` window CustomEvent; the
  AppHost triggers a full page reload for the matching app.
- **CLI — `kirocrew app dev <name> [--off] [--confirm-out-of-install-root]`**:
  toggles the flag out-of-process; the gateway watcher picks up the change
  within one poll interval, so no gateway restart is needed.

### The operator grant record

Enabling dev mode also records an **operator grant**: a file at the apps root
(`~/.kiro/crew/apps/.dev-grants.json`) mapping the app name to the ui root's
**resolved path at toggle time** (`realpath` of `<install>/ui`). It is written
**only by the dev-mode toggle** (and revoked on disable/uninstall) — never by
the gateway's startup reconcile, and never derived from `installed.json`. The
UI route requires it before serving a ui root that resolves **outside the
app's install directory**: without a grant that exactly matches the current
resolved root, out-of-install files answer `400`.

Two files, two jobs: `installed.json` `dev` (plus an internal sentinel cache,
below) drives *watching and cache headers*; the grant record is the
*authorization*. An app can write `dev: true` into its own metadata, but it
cannot mint a grant — that separation is what stops an app from pointing `ui`
at an arbitrary directory and having the UI route serve it.

Because the grant binds one exact resolved root, it is **self-invalidating**:
repointing `ui` after the toggle (an app update, a swapped link, a reinstall
under the same name) yields a root that no longer equals the granted one, and
the route answers `400` for those files until the operator re-toggles.
**Re-toggle after re-pointing** is the workflow — run the toggle again (enable
while already enabled is fine) to bind the grant to the new root. The same
applies after upgrading from a gateway version that predates the grant record:
an app already in dev mode on an out-of-install root has no grant, so its UI
answers `400` until one re-toggle.

### Refused and confirmed grants

The toggle validates the resolved ui root **before writing anything** (a
refusal never disturbs existing state):

- **Sensitive roots are never grantable.** A root that resolves *into* a
  sensitive location (credential stores, key material) or *contains* sensitive
  leaves at toggle time is refused outright with `400` and an error naming the
  resolved root — no confirmation can override this. The screen is
  **point-in-time**: it inspects the tree as it exists when the toggle runs,
  and serving afterwards re-checks only that the resolved root still equals
  the granted one. Confirming a grant approves the *tree location*, not a
  permanent screen of its future contents.
- **Out-of-install roots are refused over HTTP; confirm from the host.**
  App UI bundles run as same-origin modules with the dashboard's own
  credentials, so a request-body flag can never prove operator intent — the
  endpoint therefore has no confirmation field at all. Enabling dev mode on a
  root outside the install directory always answers `400` with
  `code: "dev_mode_out_of_install_confirmation_required"` and an error naming
  the fix: run `kirocrew app dev <name> --confirm-out-of-install-root` on the
  gateway host. The CLI is the confirmation boundary because running it
  requires the operator's own process on the host — a boundary page code
  cannot cross. This gate is a fail-closed default that blocks self-granting
  and unwitting scripted callers; the load-bearing serving guarantees remain
  the resolved-root equality binding and the sensitivity screen. Roots inside
  the install directory need no confirmation.
- **The flag is operator-only on the agent side too — three tiers.** First,
  the builtin agent deny rule
  `self-protection-dev-mode-out-of-root-confirm` refuses any agent shell
  command carrying the flag — matched both as literal text and, via the
  rule's argv floor, on the shell-de-escaped command, so quote-splitting the
  token (`--confirm-out-of-install-'root'`) is denied the same as the plain
  spelling; the `dev` subparser is built with `allow_abbrev=False`, so
  argparse rejects abbreviated spellings (`--confirm`) that would otherwise
  reach the flag without its literal text ever appearing. Second — because a
  command can *synthesize* the flag at runtime (`$(printf ...)`) so that no
  command-text scan sees it — the flag's consumption point performs a
  runtime human-vs-agent check: a process showing evidence of agent-shell
  confinement (the launcher-set sandbox marker, or on macOS the kernel's own
  Seatbelt verdict) is refused with
  `code: "dev_mode_operator_attestation_required"`. Third — because an
  environment can be scrubbed — the grant record itself
  (`~/.kiro/crew/apps/.dev-grants.json`) is sealed read-only inside the
  agent OS sandbox (Seatbelt / mount namespaces, alongside the other
  keystone ceilings), so a sandboxed process cannot mint, extend, or rewrite
  a grant no matter how the toggle is spelled; the gateway materializes the
  record at startup so the seal always has a target, and any grant-touching
  toggle from a process that cannot write the record is refused up front
  (`code: "dev_mode_grant_record_readonly"`, SEL-audited) rather than
  half-applied — use the dashboard toggle from such a process. The
  confirmation must come from the operator's own terminal, which none of
  these tiers govern.
- **Both outcomes are audited.** The unconfirmed refusal and the confirmed
  grant each emit a security event log (SEL) entry
  (`operation: dev_mode_out_of_install_grant`, outcome `denied`/`granted`,
  naming the resolved root); the granted event is written only after the
  grant record lands.

**Cost model:** dev mode is off for essentially all gateways. The
authoritative per-app state is the `installed.json` `dev` field above; to keep
the steady-state cost negligible the gateway also maintains an **internal,
unstable cache** (a small sentinel file under `~/.kiro/crew/apps/`, plus an
in-memory mirror) listing the app names currently in dev mode. The watcher
`stat()`s only that one file each second and walks a `ui/` tree solely for apps
in the set — so a gateway with no dev apps pays one `stat()` per second and
never invokes the heavier `list_apps()` walk; the in-memory mirror lets the
UI-serving hot path decide the cache header with no per-request disk IO. This
sentinel is a derived cache and **not** part of the App Kit contract: its path,
name, and format are internal implementation details, may change without
notice, and must not be read or written by app or third-party tooling — treat
`installed.json` `dev` as the only supported source of truth for the flag, and
the grant record as gateway-owned (written only through the toggle, never
directly).
