# Messaging Transport Architecture

Channel-neutral contracts used by Kiro Crew's shipped Slack, WeCom, Telegram, Discord, Webex, Teams, Weixin, iMessage, WhatsApp, and Feishu integrations — the roster in `kiro_crew/channels.py`. They also let a further channel be added without re-implementing streaming, tool approval, session identity, or rendering for each one.

- **Package:** `kiro_crew.messaging`
- **Status:** contracts plus Slack, WeCom, Telegram, Discord, Webex, Teams, Weixin, iMessage, WhatsApp, and Feishu implementations shipped. Slack's transport path is **default ON** in this fork (`messaging.use_transport`, default `true`) — opt out with `false`.

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
    def may_send_to(self, conversation_id, thread_id=None) -> bool: ...  # send-policy gate

    # configured dashboard destinations (optional; default empty)
    def configured_targets(self) -> list[ConfiguredChannelTarget]: ...
    async def resolve_configured_target(self, target_id) -> tuple[str, str | None] | None: ...

    # lifecycle (optional; default no-ops)
    async def connect(self) -> None: ...
    async def maintain(self) -> None: ...
    async def disconnect(self) -> None: ...

    # inbound
    async def receive(self, raw_envelope) -> None: ...   # parse → authorize → normalize → dispatch
    def authorize(self, msg: InboundMessage) -> bool: ... # deny-by-default
```

`TransportCapabilities` carries the quantitative differences between channels so the neutral layers can degrade gracefully instead of branching on channel type. The values below are what the four transports actually **declare** today (`<channel>/transport.py`), not the platform ceilings:

| Field | Slack | Telegram | Discord | WhatsApp |
|---|---|---|---|---|
| `streaming` | ✅ | ✅ | ✅ | ✅ (edit) |
| `edit` | ✅ | ✅ | ✅ | ✅ (20 min) |
| `reactions` | ✅ | ✅ | ✅ | ✅ |
| `rich_blocks` | ✅ (Block Kit) | ✅ | ❌ | ❌ |
| `threads` | ✅ | ✅ | ✅ | ❌ |
| `max_message_chars` | 3900 | 4000 | 1900 | 4096 |
| `max_buttons` | 10 | 25 | 25 | 0 |
| `supports_proactive_send` | ✅ | ✅ | ✅ | ✅ |

`max_buttons` is the TOTAL interactive choices a renderer may present for one `[OPTIONS:]` trailer, not a per-row layout number; overflow degrades to a numbered text list (`messaging.renderer.apply_options_cap` / `render_options_as_text`).

The dataclass carries more than the table: `files_inbound`, `files_outbound`, `table_mode`, `native_tables`, `max_message_bytes` (a UTF-8 BYTE cap, `0` = not byte-capped — Webex is the real case), `supports_session_resume`, `returns_message_id`, and `mention_grammars`.

**Honesty contract.** A declaration here is a claim other code is entitled to trust, and the docstring classifies every field as ENFORCED or ASPIRATIONAL — `test/test_capability_ledger.py` forces any new field to be classified. `max_message_chars`, `max_message_bytes`, `max_buttons`, `rich_blocks`, `table_mode`, `native_tables`, `files_outbound`, `supports_proactive_send`, `supports_session_resume`, `returns_message_id` and `mention_grammars` are ENFORCED (something behaves differently when the value changes). `streaming`, `edit`, `reactions`, `threads` and `files_inbound` are ASPIRATIONAL — declared honestly, but nothing reads them yet, so do not write code that assumes they gate anything.

The WhatsApp column is the **personal-account** channel Kiro Crew ships, paired
as a linked device over the WhatsApp Web protocol. Its numbers differ from the
Business Cloud API in both directions, so figures quoted for the Cloud API do not
apply: `max_buttons` is 0, so a trailing `[OPTIONS:]` trailer degrades to a
numbered list the user answers by typing, but equally there is no 24-hour
customer-service window, so a reminder
or a cron result can be delivered at any time, and the Web protocol exposes a
message edit the Cloud API does not, which is what lets the reply stream.

`InboundMessage` is the normalized inbound shape every channel produces: `channel_type, user_id, conversation_id, text, thread_id, attachments, is_mention`.

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
   A hook auto-approve for a **shell** command is honoured only after the
   name-grant check confirms each program name still resolves to the program
   it appears to name; a shadowed or agent-writable resolution falls through
   to the rest of the ladder instead (on a channel without a decider that
   means deny-by-default). On Windows the check cannot model the shell's
   lookup, so name-based shell auto-approve is declined entirely there.
3. Emits neutral `OutputEvent`s (`TEXT_CHUNK`, `THINKING`, `TOOL_CALL`, `PROMPT_CHOICE`, `COMPACTION`, `DONE`, `STEER_CONSUMED`) to the `Renderer`.
4. SEL-audits each approval decision.
5. Consumes **session-directive markers**. The session-bound MCP tools (`monitor_start`, `monitor_watch`, `monitor_update`, `monitor_stop`, `autonudge_stop`, `set_project`, `suggest_followup`, `ask_question`, `reset_conversation`, `section_marker` — `session_directive.DIRECTIVE_TOOLS`) are stateless: they validate their arguments and return a marker instead of resolving a session. When a `directive_consumer` is injected (`messaging.dispatch.build_directive_consumer`, bound to the turn's session key), the driver decodes the marker off `EVENT_TOOL_RESULT` and applies it through `dashboard.session_directive_apply.apply_session_directive` — the same applier the dashboard's `chat_runner` uses. A marker is honoured only when the tool call it arrived under was observed as an MCP call from `kirocrew-core` with a canonical directive-tool name (`_meta.kiro.*`), so a shell command that forges the bytes on stdout is ignored, and native sub-agent tool calls are refused. Omit the consumer and markers are ignored.
6. Frames **steering markers** before credential redaction, pairing them into `STEER_CONSUMED` events so a renderer can acknowledge a mid-turn steer.

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
    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose=""
    ) -> None: ...
    async def on_compaction(self, context_usage_pct) -> None: ...
    async def on_done(self, stop_reason="") -> None: ...
    async def on_steer_consumed(self, summary="") -> None: ...
```

Helper `chunk_text(text, max_chars)` splits long output for channels with a
small `max_message_chars`. It is a **fixed-width slice**, so use
`messaging.split.split_markdown_safe(text, limit)` instead for anything that can
contain a code block: a blind cut leaves the second part without its fence
opener, and every line in it then takes the prose branch of the channel's dialect
converter, which rewrites the `**`, `#` and `- ` *inside* the code. The shared
splitter seals each chunk with a synthetic closer and reopens the next with the
original opener line, so each part stands alone. Any caller that pre-splits before
`transport.send_message` must use it, because each part then arrives already under
the cap and the channel's own fence-safe splitter is a no-op on it — whatever cut
the caller made is the one the reader sees.

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
   `capabilities` lacks buttons, degrade to a numbered text prompt). Name the
   tool from that callback's `tool_title`/`tool_purpose`, which describe the tool
   THIS request asks about: never from a remembered earlier `on_tool_call`, which
   names the previous tool whenever a permission arrives without one of its own.
   The `options` are the ANSWERS, so an option label is not a tool name either.

4. **Wire dispatch** (`<channel>/transport_dispatch.py`): mirror
   `slack/transport_dispatch.py` — acquire the session (namespaced
   `session_key`), build context, construct the `Renderer` + `TurnDriver`, and
   `await driver.run(message)`. Reuse the neutral `TurnDriver` unchanged.

5. **Register + gate.** Add one `ChannelDescriptor` to `builtin_channel_descriptors()` in `kiro_crew/channels.py` — the single place that knows every channel — carrying `channel_type`, the `maybe_start_<channel>` boot factory, and the credential keys / `required_config` its readiness answer needs. `messaging/registry.py` owns the descriptor type and the boot/shutdown loops; it must not import a channel package (the `<channel> -> messaging` direction is pinned in `messaging/dispatch.py`), which is why the roster lives above both. `channel_type` is the ONE identity everywhere: governance member id, `MessagingTransport.channel_type`, session-key segment, config section name, dashboard badge prefix. Slack's descriptor carries `start=None` because its socket-client lifecycle is host-managed. Then route the channel's inbound events to your dispatch, and keep the channel's own `enabled` gate off until validated.

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
| `src/kiro_crew/messaging/split.py` | `split_markdown_safe` (fence-safe splitter) |
| `src/kiro_crew/messaging/dispatch.py` | dependency-direction pin, `build_directive_consumer` |
| `src/kiro_crew/messaging/registry.py` | `ChannelDescriptor`, boot/shutdown loops |
| `src/kiro_crew/channels.py` | the builtin channel roster + readiness |
| `src/kiro_crew/session_directive.py` | directive marker codec, `DIRECTIVE_TOOLS` |
| `src/kiro_crew/slack/transport.py` | Slack `MessagingTransport` |
| `src/kiro_crew/slack/renderer.py` | Slack `Renderer` |
| `src/kiro_crew/slack/transport_dispatch.py` | Slack dispatch glue |
| `src/kiro_crew/config/loader.py` | `MessagingConfig` (`use_transport`) |
