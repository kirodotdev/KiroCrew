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
the app so edits appear immediately. The recommended setup symlinks
`~/.kiro/crew/apps/<name>/ui/` to your source tree so the watcher sees edits at
the real files.

**Contract surface:**

- **`installed.json` field — `dev: bool`** (default `false`): persisted per-app
  flag. Tolerant on read (absent ⇒ `false`); reversible; no migration needed.
  Builtin apps cannot enter dev mode.
- **Endpoint — `POST /api/apps/{name}/dev`**, body `{"enabled": <bool>}`.
  Returns `{"name": <name>, "dev": <bool>}`. `400` for a non-boolean body,
  a builtin app, or an unsafe app name; `404` if the app is not installed.
  Behind the standard gateway auth; emits an `app_dev_mode` SEL audit event.
- **WebSocket event — `app_reload`**, payload `{"app": <name>, "ts": <float>}`.
  Re-dispatched to the frontend as the `mc:app-reload` window CustomEvent; the
  AppHost triggers a full page reload for the matching app.
- **CLI — `kirocrew app dev <name> [--off]`**: toggles the flag out-of-process;
  the gateway watcher picks up the change within one poll interval, so no
  gateway restart is needed.

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
`installed.json` `dev` as the only supported source of truth.
