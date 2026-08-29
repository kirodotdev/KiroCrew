# API Reference — KiroCrew Gateway API & Client

Reference for the KiroCrew Gateway HTTP and WebSocket APIs, and how apps consume
them.

How you talk to the Gateway depends on where your code runs:

- **Dashboard UI pages (TypeScript/React)** — use the `@kirocrew/app-sdk` hooks
  (`useAppApi`, `useAppEvents`, …). You do **not** `npm install` this package;
  the dashboard host provides it at runtime through its import map (the bare
  specifier `@kirocrew/app-sdk` resolves to the host's vendored copy via
  `window.__kirocrew_modules`). See
  [getting-started.md](getting-started.md) and the [App SDK Hooks](#app-sdk-hooks)
  section below.
- **Python apps / external CLI tools / services** — use the standalone
  `kirocrew-client` package (`pip install kirocrew-client`). It is async
  (`aiohttp`) and has no dependency on the KiroCrew main package. See the
  [Python Client](#python-client) section.
- **Node.js / Electron apps** — call the Gateway REST/WS endpoints directly via
  `fetch()` / a WebSocket. The full endpoint list is in
  [Gateway REST API Endpoints](#gateway-rest-api-endpoints).

There is no published TypeScript gateway-client npm package. The `kirocrew-client`
method names below describe the canonical Gateway API surface — the same
endpoints any client (including raw `fetch`) talks to.

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

`useAppApi()` returns a client whose methods (`get`, `post`, `put`, `patch`,
`del`) call the Gateway endpoints listed below, scoped to the `permissions.api`
paths your `app.json` declares. The host injects auth automatically.

For the full hook list see [getting-started.md](getting-started.md#app-sdk-hooks).

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
| `ParsedOptions` | type | `{ text, options, multi, isPlan }` |
| `FollowUpDerivation` | type | `{ followUpOptions, followUpIsPlan }` |
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

The sections below document the canonical Gateway API surface as exposed by the
`kirocrew-client` Python package (see the [Python Client](#python-client)
section for the constructor and full method list). Method names are also a
convenient way to refer to each endpoint — the same endpoints any client
(including raw `fetch`) talks to.

When `app_name` is set and no explicit auth is provided, the client auto-reads
the app secret from `~/.kiro/crew/apps/{name}/.app_secret` and exchanges it
for a short-lived token via `POST /api/apps/{name}/token`.

### Authentication

| Method | Returns | Description |
|--------|---------|-------------|
| `authenticate()` | `Promise<boolean>` | Exchange app secret for token (auto-called if appName set) |
| `setToken(token)` | `void` | Manually set auth token on both HTTP and WS clients |

### Connection

| Method | Returns | Description |
|--------|---------|-------------|
| `ping()` | `Promise<boolean>` | Check if Gateway is reachable |
| `getStatus()` | `Promise<GatewayStatus>` | Gateway health (version, uptime, slots, provider) |
| `getSystemInfo()` | `Promise<SystemInfo>` | CPU, memory, disk metrics |

### Chat Slots

| Method | Returns | Description |
|--------|---------|-------------|
| `createSlot(name, agent?)` | `Promise<SlotInfo>` | Create a new chat session |
| `listSlots()` | `Promise<SlotInfo[]>` | List all active sessions |
| `deleteSlot(slotId)` | `Promise<void>` | Remove a session |
| `getSlotHistory(slotId, limit?)` | `Promise<{messages, total}>` | Get slot message history |
| `sendMessage(slotId, message)` | `Promise<void>` | Send a message (validates length, auto-flushes pending context) |

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
| `onRawMessage(cb)` | `() => void` | All raw WebSocket messages |

All `on*` methods return an unsubscribe function.

WebSocket event types: `chat_chunk`, `chat_done`, `chat_message`, `chat_error`,
`tool_call`, `notification`, `slots`, `slot_title`, `dashboard`, `log`, `refresh`,
`approval`, `subagent_done`, `task_update`, `task_complete`, `proactive_notification`,
`app_reload`, `error`.

### Subagents

| Method | Returns | Description |
|--------|---------|-------------|
| `spawn(task, agent?)` | `Promise<string>` | Spawn a background subagent |
| `spawnMany(tasks, agents?)` | `Promise<string[]>` | Spawn multiple subagents in parallel |
| `listSubagents()` | `Promise<SubagentInfo[]>` | List all subagents |
| `getSubagentStatus(id)` | `Promise<SubagentResult>` | Get subagent output |

### Cron Jobs

| Method | Returns | Description |
|--------|---------|-------------|
| `addCron(name, options)` | `Promise<CronJob>` | Create a scheduled job |
| `listCrons()` | `Promise<CronJob[]>` | List all cron jobs |
| `updateCron(id, options)` | `Promise<CronJob>` | Update a cron job |
| `removeCron(id)` | `Promise<void>` | Delete a cron job |
| `pauseCron(id)` | `Promise<void>` | Pause without deleting |
| `resumeCron(id)` | `Promise<void>` | Resume a paused job |

#### Watching something without paying for a model call (`kiro_crew.irq`)

> **Provisional surface.** `kiro_crew.irq` has exactly one probe today
> (`pr_watch`). The ~15 sibling pollers this abstraction was derived from
> cannot migrate onto it yet, so a second real consumer has not yet tested the
> contract. Treat the shapes below as subject to change until one has: build on
> them, but expect `Observation` / `Tick` to gain fields, and pin the Kiro Crew
> version your app was tested against.

An app that needs to keep an eye on an external thing — a deploy, a ticket, a
queue depth — should not schedule an **agent** cron to go look. That spends a
full model turn per check, and on a quiet subject every one of those turns says
"nothing changed".

Schedule a **script** cron instead and build it on `kiro_crew.irq`, the
interrupt controller. The script runs in a subprocess with no model call at
all; a quiet tick is free. Only an unexpected observation raises a wake, and the
wake is delivered into the session that armed the cron as a real agent turn.
Full design: `docs/system-specs/features/agent-interrupt-controller.md`.

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
            # Nothing improves by waiting -> NMI bypasses coalescing.
            obs.append(Observation("rollback", Severity.NMI,
                                   f"{self.env} rolled back."))
        for stage in status.failed_stages:
            obs.append(Observation(f"stage:{stage}", Severity.WAKE,
                                   f"{self.env}: stage {stage} failed."))
        return Tick(epoch=status.id, observations=obs,
                    pending=status.running_stages)


def watch(ctx):                                   # cron entry point
    run(ctx, DeployProbe())
```

Register it with `addCron(name, { script: "<crons dir>/your_probe.py:watch",
every: 300, timeout: 120, message: JSON.stringify({ env: "prod" }) })`. Cron
scripts must live under the config directory's `crons/`, and the cron must be
armed **from the session that should receive the wake** — the cron system
captures the calling session at creation time.

Rules:

- **Never raise `Skip` / `Report` / `Done`.** Return data; the kernel decides.
  It is the only place a verdict is raised.
- A failed observation returns `Tick(fetch_ok=False)`, never an empty `Tick` —
  an empty tick reads as "nothing is wrong".
- Use `Severity.NMI` only for what genuinely cannot improve by waiting. Using
  it to mean "important" defeats coalescing.
- Supply an `epoch` when the subject has an identity token. Without one there
  are no resets, so a re-triggered subject inherits the previous run's masks.
- Filter out conditions the operator already knows about (a check red on the
  base branch, a known-degraded dependency) in your own `observe()` — do not
  return them. An earlier revision carried an `expected=True` flag for this; it
  was removed because nothing read the state it recorded.
- Keep `observe()` to one bounded call. This half must stay fast and cheap.
- `coalesce_secs=0` turns coalescing off — pass it to `run()`, or return it from
  your probe's `tuning()` when it should come from the cron message. Do that when
  you would rather be woken early than woken once: coalescing costs at least one
  cron interval of latency, because a window cannot open and fire within the
  same tick.

### Lessons

| Method | Returns | Description |
|--------|---------|-------------|
| `addLesson(rule, category, scope?)` | `Promise<void>` | Save a learned rule |
| `listLessons()` | `Promise<Lesson[]>` | List all lessons |
| `removeLesson(query)` | `Promise<void>` | Remove matching lessons |

### Notifications

| Method | Returns | Description |
|--------|---------|-------------|
| `sendNotification(text, options?)` | `Promise<void>` | Send via Slack or dashboard |
| `listNotifications()` | `Promise<{notifications}>` | List notifications |
| `ackNotifications()` | `Promise<void>` | Acknowledge all notifications |

### Approvals

| Method | Returns | Description |
|--------|---------|-------------|
| `approveAction(slotId, taskId)` | `Promise<void>` | Approve a pending tool action |
| `rejectAction(slotId, taskId)` | `Promise<void>` | Reject a pending tool action |
| `resolveApproval(approvalId, approved)` | `Promise<void>` | Resolve an approval by ID |
| `getApprovalMode()` | `Promise<'auto'\|'interactive'>` | Get current approval mode |
| `setApprovalMode(mode)` | `Promise<void>` | Set approval mode |

### Models

| Method | Returns | Description |
|--------|---------|-------------|
| `listModels()` | `Promise<ModelInfo[]>` | List available LLM models |
| `setSlotModel(slotId, model)` | `Promise<void>` | Set model for a slot |

### MCP Servers

| Method | Returns | Description |
|--------|---------|-------------|
| `listMcpServers()` | `Promise<McpServerInfo[]>` | List registered MCP servers |
| `registerMcpServer(def)` | `Promise<void>` | Register an MCP server (requires name + command) |
| `removeMcpServer(name)` | `Promise<void>` | Remove an MCP server |
| `registerAppMcp(name, entry)` | `Promise<void>` | Write MCP entry to `~/.kiro/crew/mcp.json` (Node.js only) |
| `unregisterAppMcp(name)` | `Promise<void>` | Remove MCP entry from `~/.kiro/crew/mcp.json` (Node.js only) |

### Agent & Skill Installation (Node.js only)

| Method | Returns | Description |
|--------|---------|-------------|
| `installAgentConfig(name, config)` | `void` | Install agent JSON to `~/.kiro/agents/` (merges mcpServers) |
| `removeAgentConfig(name)` | `void` | Remove agent config |
| `installSkill(name, srcDir)` | `void` | Copy skill directory to `~/.kiro/crew/skills/` |
| `removeSkill(name)` | `void` | Remove skill directory |

### Agent Runtime

| Method | Returns | Description |
|--------|---------|-------------|
| `dispatchAgent(agent, prompt)` | `Promise<TaskResult>` | Run agent synchronously |
| `dispatchAgentAsync(agent, prompt)` | `Promise<string>` | Run agent in background |
| `getTaskResult(taskId)` | `Promise<TaskResult>` | Poll task status |

### Gateway Config

| Method | Returns | Description |
|--------|---------|-------------|
| `getGatewayConfig(key)` | `Promise<Record<string, unknown>>` | Read gateway config section |
| `setGatewayConfig(key, value)` | `Promise<void>` | Write gateway config section |

### App Storage

| Method | Returns | Description |
|--------|---------|-------------|
| `getAppDataDir()` | `string` | App-scoped data directory path |
| `getAppConfig()` | `Promise<Record<string, unknown>>` | Read app config via REST |
| `setAppConfig(config)` | `Promise<void>` | Write app config via REST |

### Memory

| Method | Returns | Description |
|--------|---------|-------------|
| `memorySearch(query, topK?)` | `Promise<MemoryResult[]>` | Semantic memory search |

### Context Injection

Silent background context for LLM — content appears in the next user-initiated turn without triggering a response or showing a visible message.

| Method | Returns | Description |
|--------|---------|-------------|
| `injectContext(slotId, content, options?)` | `Promise<void>` | Inject context (null slotId = buffer locally) |
| `flushPendingContext(slotId)` | `Promise<void>` | Flush buffered entries to a slot |
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

### Notes

`POST /api/chat/slots/{slot}/note` drops a short declarative line into a chat that is both visible in the transcript immediately and known to the agent on the user's next message — without firing an LLM turn. Context injection alone is silent; a transcript append alone is invisible to the model, because a live provider forwards only the new user message. The note endpoint does both writes against one slot.

Body: `{ content, source?, maxAge?, ephemeral?, visibleOnly? }`. A note does both writes by default. `visibleOnly: true` writes ONLY the visible transcript row and builds no pending-context entry — for a breadcrumb that should be readable forever but must not be narrated into the agent's next turn, which is what a durable record of a local UI action needs. The visible line is appended as `role: "inject"` with `cls: "reconcile-note"`, and its content is redacted (credentials, exfiltration URLs) before it reaches the transcript. `maxAge` defaults to 24h for the context half when the key is omitted, so a note nobody follows up on expires instead of attaching to an unrelated message later; it has no effect under `visibleOnly`, which writes no context half to expire. An explicit null means no expiry, the same as it does on `/context` — the two endpoints share the field and do not give it opposite meanings. The same `source`/`maxAge`/`content` constraints above apply.

Returns `{ ok, appended, visibleDeferred, deliveryConditional, contextSkipped, pending }`. When the source's per-source context cap is full the request is **not** rejected: the visible line is still written and `contextSkipped` is true, because the cap protects the context queue rather than the transcript. If a turn is already running the note is held until that turn ends -- `appended` is false and `visibleDeferred` is true -- so that it lands on the next turn rather than the one it was written during. Ordering is preserved, and `deliveryConditional` is true whenever a note is held -- because a hold is delivered only if the slot still routes to the SAME session when the turn ends. An unbound slot can acquire a foreign binding while the note waits (a cron result or workflow injection claims an empty `linked_session_key` with no running gate), and both the transcript path and the next turn's session resolve that binding at flush time rather than at the POST. When that happens BOTH halves of the note are dropped rather than retargeted, because writing them would surface content authorized for one conversation inside another; the drop is recorded in the security-event log. So a 200 with `visibleDeferred: true` promises ordering against the running turn, not that the note will certainly be written. `pending` counts held entries as well as queued ones.

**A 200 means "accepted for this gateway lifetime", not durable delivery.** Both halves of a note live in memory only -- the held visible line and the queued context, the latter exactly as `/context`'s queue has always behaved -- so a gateway restart between the acknowledgement and the next turn drops them. A caller that needs a note to survive a restart must re-post it; `visibleDeferred: true` promises ordering against the running turn, not persistence.

### Proxy Authentication (Server-side)

Verify that an incoming request was signed by the KiroCrew gateway reverse proxy. Use in app backends to authenticate proxied requests.

| Function | Returns | Description |
|----------|---------|-------------|
| `verifyProxyRequest(req, appName, opts?)` | `boolean` | Verify HMAC signature on any Node.js request object |

Options: `{ secret?: string, maxAgeSecs?: number }`

---

## Python Client

Standalone async client using `aiohttp` — `pip install kirocrew-client`. Covers
the full Gateway API surface documented above.

```python
from kirocrew_client import KiroCrewClient

async with KiroCrewClient(app_name="my-app") as mc:
    ok = await mc.ping()
    slots = await mc.list_slots()
```

### Constructor

```python
KiroCrewClient(
    base_url="",              # default: http://localhost:{KIROCREW_PORT or 5476}
    token="",                 # optional for localhost
    app_name="",              # for app-scoped storage & auto-auth
    timeout=30,               # request timeout seconds
    max_retries=3,            # retry count
    retry_base_delay=1.0,     # base delay for backoff
    message_length_limit=40000,
    on_auth_expired=None,     # async callback returning new token
)
```

### Method Reference

Method names use `snake_case` per Python convention. The left column is the
canonical API-surface name used in the sections above:

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
| `spawn(task, agent?)` | `spawn(task, agent="")` |
| `spawnMany(tasks, agents?)` | `spawn_many(tasks, agents=None)` |
| `listSubagents()` | `list_subagents()` |
| `getSubagentStatus(id)` | `get_subagent_status(id)` |
| `addCron(name, opts)` | `add_cron(name, **opts)` |
| `listCrons()` | `list_crons()` |
| `updateCron(id, opts)` | `update_cron(id, **opts)` |
| `removeCron(id)` | `remove_cron(id)` |
| `pauseCron(id)` | `pause_cron(id)` |
| `resumeCron(id)` | `resume_cron(id)` |
| `addLesson(rule, cat, scope?)` | `add_lesson(rule, cat, scope="")` |
| `listLessons()` | `list_lessons()` |
| `removeLesson(query)` | `remove_lesson(query)` |
| `sendNotification(text, opts?)` | `send_notification(text, **opts)` |
| `listNotifications()` | `list_notifications()` |
| `ackNotifications()` | `ack_notifications()` |
| `approveAction(slot, task)` | `approve_action(slot, task)` |
| `rejectAction(slot, task)` | `reject_action(slot, task)` |
| `resolveApproval(id, ok)` | `resolve_approval(id, ok)` |
| `getApprovalMode()` | `get_approval_mode()` |
| `setApprovalMode(mode)` | `set_approval_mode(mode)` |
| `listModels()` | `list_models()` |
| `setSlotModel(slot, model)` | `set_slot_model(slot, model)` |
| `getGatewayConfig(key)` | `get_gateway_config(key)` |
| `setGatewayConfig(key, val)` | `set_gateway_config(key, val)` |
| `listMcpServers()` | `list_mcp_servers()` |
| `registerMcpServer(def)` | `register_mcp_server(name, cmd, args?, env?)` |
| `removeMcpServer(name)` | `remove_mcp_server(name)` |
| `registerAppMcp(name, entry)` | `register_app_mcp(name, *, url?, cmd?, ...)` |
| `unregisterAppMcp(name)` | `unregister_app_mcp(name)` |
| `installAgentConfig(name, cfg)` | `install_agent_config(name, cfg)` |
| `removeAgentConfig(name)` | `remove_agent_config(name)` |
| `installSkill(name, dir)` | `install_skill(name, dir)` |
| `removeSkill(name)` | `remove_skill(name)` |
| `dispatchAgent(agent, prompt)` | `dispatch_agent(agent, prompt)` |
| `dispatchAgentAsync(agent, prompt)` | `dispatch_agent_async(agent, prompt)` |
| `getTaskResult(id)` | `get_task_result(id)` |
| `getAppDataDir()` | `get_app_data_dir()` → `Path` |
| `getAppConfig()` | `get_app_config()` |
| `setAppConfig(cfg)` | `set_app_config(cfg)` |
| `memorySearch(q, topK?)` | `memory_search(q, top_k=8)` |
| `injectContext(slot, content, opts?)` | `inject_context(slot, content, *, source?, ephemeral?, max_age?)` |
| `flushPendingContext(slot)` | `flush_pending_context(slot)` |
| `setDefaultSlot(slot)` | `set_default_slot(slot)` |

**Proxy Authentication (standalone functions):**

| API surface | Python |
|-----------|--------|
| `verifyProxyRequest(req, appName, opts?)` | `verify_proxy_request(request, app_name, *, secret?, max_age_secs?)` |
| — | `verify_proxy_request_raw(header, method, path, app_name, ...)` |

---

## AppManifest

Validate and serialize app.json manifests, via the `kirocrew-client` package.

```python
from kirocrew_client import AppManifest

m = AppManifest.from_dict({"name": "my-app", "version": "1.0.0", ...})
errors = m.validate()   # list[str] — empty if valid
data = m.to_dict()
```

## AppLifecycle

Manage app installation via the Gateway REST API.

```python
from kirocrew_client import KiroCrewClient, AppLifecycle

async with KiroCrewClient() as mc:
    lifecycle = AppLifecycle(mc)
    await lifecycle.install("/path/to/my-app")
    await lifecycle.enable("my-app")
    await lifecycle.disable("my-app")
    await lifecycle.uninstall("my-app")
    apps = await lifecycle.list()
```

## GatewayManager

Manage the KiroCrew Gateway process (start, stop, health check).

```python
from kirocrew_client import GatewayManager

gm = GatewayManager(port=5476)
await gm.start()
healthy = await gm.is_healthy()
await gm.stop()
```

---

## Error Handling

All `kirocrew-client` errors are `KiroCrewError` instances with `code`,
`message`, `status`, `body`.

| Code | Trigger | Retried? |
|------|---------|----------|
| `AUTH_REQUIRED` | Remote connection without token | No |
| `AUTH_EXPIRED` | 401/403 response | No (calls on_auth_expired if set) |
| `VALIDATION_ERROR` | Invalid input | No |
| `NOT_FOUND` | 404 response | No |
| `RATE_LIMITED` | 429 response | Yes (Retry-After or backoff) |
| `SERVER_ERROR` | 5xx response | Yes (exponential backoff) |
| `NETWORK_ERROR` | Timeout or connection failure | Yes (exponential backoff) |
| `WS_DISCONNECTED` | WebSocket not connected | No |

```python
from kirocrew_client import KiroCrewError

try:
    await mc.send_message("slot-1", "hello")
except KiroCrewError as e:
    print(e.code, e.message, e.status)
```

---

## Gateway REST API Endpoints

The `useAppApi()` hook and the `kirocrew-client` package wrap these Gateway
endpoints. Apps can also call them directly via `fetch()`.

### App Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/apps` | List all installed apps |
| GET | `/api/apps/registry` | List available apps from registry |
| GET | `/api/apps/blob?repo=&path=&ref=` | Proxy images from a registry app's git repo |
| POST | `/api/apps/install` | Install from local path |
| POST | `/api/apps/register` | Register a self-managed app |
| POST | `/api/apps/registry/install` | Install from registry |
| GET | `/api/apps/{name}` | Get app details |
| GET | `/api/apps/{name}/manifest` | Get app manifest |
| GET/PUT | `/api/apps/{name}/config` | Read/write app config |
| POST | `/api/apps/{name}/update` | Update installed app |
| POST | `/api/apps/{name}/uninstall` | Uninstall app |
| POST | `/api/apps/{name}/enable` | Enable app |
| POST | `/api/apps/{name}/disable` | Disable app |
| POST | `/api/apps/{name}/dev` | Toggle dev mode (live reload) — body `{"enabled": bool}` |
| POST | `/api/apps/{name}/open` | Launch app via openCommand |
| GET | `/apps/{name}/ui/{path}` | Serve app UI bundle files |
| * | `/apps/{name}/api/{path}` | Reverse proxy to app backend (HMAC-signed) |

### Reverse Proxy Authentication

The gateway signs each proxied request with `X-KiroCrew-Proxy: <timestamp>:<hmac-sha256>`. The
HMAC is computed over the message `timestamp:method:/api/path[?query]:sha256(body)` using the
app secret as the key, where `sha256(body)` is the hex SHA-256 digest of the raw request body
(an empty body hashes the empty byte string, `e3b0c442...`). Binding the body hash means a
tampered body invalidates the signature. Backends verify with a constant-time comparison and
reject requests whose timestamp is not within ±60s of now.

Python app backends verify this with `kirocrew-client`:

```python
from kirocrew_client import verify_proxy_request
if not verify_proxy_request(request, 'my-app'): return Response(status=401)
```

Node.js app backends can verify the signature directly: compute
`HMAC-SHA256(timestamp:method:/api/path[?query]:sha256(body), app_secret)` and compare against
the value in the `X-KiroCrew-Proxy` header (constant-time), rejecting stale timestamps.

> **Breaking change (body-bound signature):** `verify_proxy_request` /
> `verify_proxy_request_raw` in the `kirocrew-client` package MUST be regenerated in lockstep
> to bind `sha256(body)` while keeping the constant-time compare and ±60s freshness. A gateway
> that signs body-bound HMACs will fail verification against any deployed old verifier, so the
> client release must ship together with this change.

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
