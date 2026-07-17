# Messaging Transport Architecture

Channel-neutral contracts that let KiroCrew talk to Slack today and to other
chat channels (Telegram, Discord, WhatsApp, Teams, …) tomorrow — without
re-implementing streaming, tool-approval, session identity, or rendering for
each one.

- **Package:** `kiro_crew.messaging`
- **Status:** contracts + Slack implementation shipped; **default ON** in this
  fork (`messaging.use_transport`, default `true`) — opt out with `false`.

## Why

Historically the Slack turn loop (`slack/handler.py::handle_message`, 4000+
lines) hard-codes streaming, rendering, auth, session lifecycle, and the
tool-approval ladder. Adding a new channel meant forking that surface. The
messaging package extracts the **channel-neutral** parts so a new channel only
implements two small interfaces and inherits everything else.

**Dependency direction is one-way:** `slack` / `dashboard` → `messaging`, never
the reverse. `kiro_crew.messaging` imports nothing from `kiro_crew.slack`.

## The three layers

```
                 ┌─────────────────────────────────────────────┐
 inbound event   │ Layer 1: MessagingTransport (per channel)    │
  ───────────────▶  receive() → authorize() → normalize          │
                 │            → InboundMessage                    │
                 └───────────────────────┬─────────────────────┘
                                         │ dispatch
                 ┌───────────────────────▼─────────────────────┐
 provider stream │ Layer 2: TurnDriver (channel-neutral)        │
  ◀──────────────▶  redact → approval ladder → OutputEvent        │
                 │            → Renderer.dispatch()               │
                 └───────────────────────┬─────────────────────┘
                                         │ on_* callbacks
                 ┌───────────────────────▼─────────────────────┐
 channel API     │ Layer 2b: Renderer (per channel)             │
  ◀──────────────▶  on_text_chunk / on_tool_call / on_prompt_    │
                 │  choice / on_compaction / on_done              │
                 └─────────────────────────────────────────────┘

 Layer 3 (cross-cutting): ChannelLink + SessionMap namespacing
   maps (channel, conversation, thread) ⇄ a namespaced session key.
```

### Layer 1 — `MessagingTransport` (inbound + outbound adapter)

`kiro_crew/messaging/transport.py`. One implementation per channel.

```python
class MessagingTransport(ABC):
    channel_type: str = ""            # "slack" | "telegram" | ...
    capabilities: TransportCapabilities

    # outbound
    async def send_message(self, conversation_id, content, thread_id=None) -> str: ...
    async def resolve_conversation(self, user_id) -> str: ...
    async def fetch_history(self, conversation_id, thread_id=None) -> list[InboundMessage]: ...

    # lifecycle (optional; default no-ops)
    async def connect(self) -> None: ...
    async def maintain(self) -> None: ...
    async def disconnect(self) -> None: ...

    # inbound
    async def receive(self, raw_envelope) -> None: ...   # parse → authorize → normalize → dispatch
    def authorize(self, msg: InboundMessage) -> bool: ... # deny-by-default
```

`TransportCapabilities` carries the quantitative differences between channels so
the neutral layers can degrade gracefully instead of branching on channel type:

| Field | Slack | Telegram | Discord | WhatsApp |
|---|---|---|---|---|
| `streaming` | ✅ | via draft API | ❌ | ❌ |
| `edit` | ✅ | ✅ | ✅ | ❌ |
| `reactions` | ✅ | limited | ✅ | ❌ |
| `rich_blocks` | ✅ (Block Kit) | ✅ | ✅ (embeds) | ❌ |
| `threads` | ✅ | reply_to | ✅ | ❌ |
| `max_message_chars` | ~40000 | 4096 | 2000 | 4096 |
| `max_buttons` | many | ~8/row | 5/row | 3 |
| `supports_proactive_send` | ✅ | ✅ | ✅ | ❌ (24h window) |

`InboundMessage` is the normalized inbound shape every channel produces:
`channel_type, user_id, conversation_id, text, thread_id, is_mention`.

### Layer 2 — `TurnDriver` (channel-neutral turn loop)

`kiro_crew/messaging/driver.py`. Shared by every channel — you do **not**
reimplement this. It consumes the provider (LLM) event stream and:

1. **Redacts** every text/option (exfiltration URLs + credentials) before it
   reaches a renderer or channel.
2. Runs the **approval ladder** on tool-permission requests:
   `APPROVAL_AUTO` / `APPROVAL_TRUST` / `APPROVAL_TRUST_READS` / `APPROVAL_INTERACTIVE`
   (default `APPROVAL_INTERACTIVE` = deny-by-default unless a decider resolves).
   Injected predicates preserve hook auto-approval (`spawn_run`) and
   per-session Trust without the driver depending on any channel module.
3. Emits neutral `OutputEvent`s (`TEXT_CHUNK`, `THINKING`, `TOOL_CALL`,
   `PROMPT_CHOICE`, `COMPACTION`, `DONE`) to the `Renderer`.
4. SEL-audits each approval decision.

```python
driver = TurnDriver(provider, renderer, approval_mode=..., decider=...)
accumulated = await driver.run(message)
```

### Layer 2b — `Renderer` (per channel)

`kiro_crew/messaging/renderer.py`. One implementation per channel. The
`TurnDriver` calls these; you map them onto the channel's API:

```python
class Renderer(ABC):
    async def on_turn_start(self) -> None: ...          # (optional) ack/working indicator
    async def on_text_chunk(self, text) -> None: ...
    async def on_thinking(self, text) -> None: ...
    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose="") -> None: ...
    async def on_prompt_choice(self, options, request_id) -> None: ...
    async def on_compaction(self, context_usage_pct) -> None: ...
    async def on_done(self, stop_reason="") -> None: ...
```

Helper `chunk_text(text, max_chars)` splits long output for channels with a
small `max_message_chars`.

### Layer 3 — session identity (`ChannelLink` + SessionMap)

`kiro_crew/messaging/link.py`. Session keys are **namespaced by channel** so two
channels never collide: `session_key("slack", conversation)` →
`"slack:<conversation>"`. Use `canonical_key()` for SessionMap lookups. Legacy
bare Slack keys are migrated via `legacy_key()` / `is_legacy_slack_key()`.

## How Slack uses it (and how the default-ON flag works)

Slack is the reference implementation:
- **Inbound:** `slack/transport.py::SlackTransport` (owner-only deny-by-default
  `authorize`, bot-drop, SEL audit on every denial including empty `user_id`).
- **Rendering:** `slack/renderer.py::SlackRenderer` — behavior-faithful port of
  the native streaming loop (stream/throttle/rotation-fallback, tool-timer,
  thread-status lifecycle, Block Kit approval buttons via `SlackApprovalDecider`).
- **Dispatch glue:** `slack/transport_dispatch.py::handle_message_transport` —
  session acquire → context build → `TurnDriver.run()` → `SlackRenderer`.

**Feature flag (default ON).** `messaging.use_transport` gates the path in
`slack/events.py` (the main inbound route), *after* the shared auth check:

```
1. auth: is_owner(sender) or is_allowed_user(sender)         # both paths
2. if messaging.use_transport is True (default):  → transport path → return
3. else (opt-out, use_transport=false):           → native handle_message
```

In this fork the flag defaults to `true` (`MessagingConfig.use_transport` and
`config-baseline.json` both ship `true`, and `orch._cfg.messaging` is always
populated), so the transport path handles every install's Slack messages unless
an operator explicitly sets `messaging.use_transport = false` in config (plus a
gateway restart) to fall back to the native `handle_message` loop.

> Tool-approval on the transport path is gated by the same
> YOLO/`SafetyOverride` TTL resolver (`_resolve_approval_mode`) the native path
> uses — deny-by-default unless auto-approve is explicitly active — and the
> upstream `is_owner`/`is_allowed_user` check protects both paths.

## What a new channel inherits for free

Implement only Layer 1 (`Transport`) + Layer 2b (`Renderer`) and register it.
You automatically get: LLM-output redaction, the SEL-audited approval ladder,
namespaced session identity + per-conversation state, capability-driven
graceful degradation, and long-message chunking.

## Add a new channel — step by step

1. **Declare capabilities.** Build a `TransportCapabilities` describing the
   channel's limits (char cap, buttons, streaming/edit/reactions, proactive
   send). The neutral layers read these instead of branching on channel type.

2. **Implement `MessagingTransport`** (`<channel>/transport.py`):
   - `channel_type = "<name>"`, `capabilities = <caps>`
   - `send_message` / `resolve_conversation` / `fetch_history` against the
     channel API
   - `authorize(msg)` — **deny-by-default**; allow only known/owner users
   - `receive(raw)` — parse the channel's inbound payload → build an
     `InboundMessage` → `authorize()` → hand off to dispatch (drop bot echoes)
   - optionally `connect`/`maintain`/`disconnect` for webhook/poll lifecycle

3. **Implement `Renderer`** (`<channel>/renderer.py`): map each `on_*`
   callback onto the channel API. Use `chunk_text()` for `max_message_chars`;
   render `on_prompt_choice` with the channel's interactive controls (or, if
   `capabilities` lacks buttons, degrade to a numbered text prompt).

4. **Wire dispatch** (`<channel>/transport_dispatch.py`): mirror
   `slack/transport_dispatch.py` — acquire the session (namespaced
   `session_key`), build context, construct the `Renderer` + `TurnDriver`, and
   `await driver.run(message)`. Reuse the neutral `TurnDriver` unchanged.

5. **Register + gate.** Add an opt-in config flag (like `messaging.use_transport`)
   and route the channel's inbound events to your dispatch. Keep it default-off
   until validated.

6. **Lock behavior with a transcript-style test**: drive a scripted provider
   event stream through the real turn (see `test/test_slack_renderer.py`) and
   assert the ordered channel-API call sequence, so future refactors can't
   silently change UX.

## Key files

| Path | Role |
|---|---|
| `src/kiro_crew/messaging/transport.py` | `MessagingTransport`, `TransportCapabilities`, `InboundMessage` |
| `src/kiro_crew/messaging/driver.py` | `TurnDriver` + approval ladder + redaction |
| `src/kiro_crew/messaging/renderer.py` | `Renderer` ABC, `OutputEvent`, `chunk_text` |
| `src/kiro_crew/messaging/link.py` | `ChannelLink`, `session_key`, `canonical_key` |
| `src/kiro_crew/slack/transport.py` | Slack `MessagingTransport` |
| `src/kiro_crew/slack/renderer.py` | Slack `Renderer` |
| `src/kiro_crew/slack/transport_dispatch.py` | Slack dispatch glue |
| `src/kiro_crew/config/loader.py` | `MessagingConfig` (`use_transport`) |
