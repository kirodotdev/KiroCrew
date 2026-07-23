# Messaging Transport Module

Last Updated: 2026-07-13 (Initial module spec: channel-neutral `kiro_crew.messaging` package — Layer 1 `MessagingTransport`/`TransportCapabilities`/`InboundMessage`, Layer 2 `TurnDriver` approval ladder, Layer 2b `Renderer`/`OutputEvent`/`chunk_text`, Layer 3 session-key namespacing + ConversationState generations; Slack reference impl + `messaging.use_transport` flag, default ON in KiroCrew)

## Overview

`kiro_crew.messaging` is the channel-neutral transport abstraction that lets KiroCrew talk to Slack today and to other chat channels (Telegram, Discord, WhatsApp, Teams, …) tomorrow without re-implementing streaming, tool-approval, session identity, or rendering for each one. It extracts the channel-neutral core of the historically monolithic Slack turn loop (`slack/handler.py::handle_message`) so a new channel implements only two small interfaces (a `MessagingTransport` + a `Renderer`) and inherits everything else.

**Dependency direction is one-way:** `slack` / `dashboard` → `messaging`, never the reverse. The `kiro_crew.messaging` package imports nothing from `kiro_crew.slack` or `kiro_crew.dashboard`; its only first-party dependencies are the shared lower-level helpers — `acp.types` event constants, the `security` redactors (`redact_credentials` / `redact_exfiltration_urls`), and `sel` for audit.

**Status:** contracts + Slack reference implementation shipped, gated behind the `messaging.use_transport` config flag (default `true` in KiroCrew — the transport abstraction is the canonical path). When off, the native `handle_message` path runs unchanged.

## Architecture — the three layers

```
 inbound event   Layer 1: MessagingTransport (per channel)
  ─────────────▶   receive() → drop bots → normalize → authorize()
                   → InboundMessage → dispatch callback
                            │
 provider stream  Layer 2: TurnDriver (channel-neutral)
  ─────────────▶   redact → approval ladder → OutputEvent
                   → Renderer.dispatch()
                            │
 channel API      Layer 2b: Renderer (per channel)
  ◀────────────    on_text_chunk / on_thinking / on_tool_call /
                   on_prompt_choice / on_compaction / on_done

 Layer 3 (cross-cutting): ChannelLink + session-key namespacing
   f"{channel_type}:{conversation_id}" ⇄ legacy bare Slack thread_ts
```

## Files

| File | Purpose |
|------|---------|
| `messaging/__init__.py` | Package facade re-exporting the public contracts, approval-mode constants, and Layer-3 helpers |
| `messaging/transport.py` | **Layer 1** — `MessagingTransport` ABC + the `TransportCapabilities` and `InboundMessage` value objects (stdlib-only) |
| `messaging/driver.py` | **Layer 2** — `TurnDriver` (channel-neutral turn loop), approval-mode constants, `_redact` helper |
| `messaging/renderer.py` | **Layer 2b** — `Renderer` ABC, `OutputEvent`, output-kind constants + `OUTPUT_KINDS`, `chunk_text` helper |
| `messaging/link.py` | **Layer 3** — session-key namespacing (`session_key`/`canonical_key`/`legacy_key`/`is_legacy_slack_key`) + `ChannelLink` + DM-scope key derivation / `should_rotate_generation` |
| `messaging/conversation.py` | `ConversationState` — per-conversation rotating *generation* bookkeeping (advanced by `/new` and idle/daily reset), seeded from the persisted session map |
| `slack/transport.py` | Slack reference `MessagingTransport` (`SlackTransport`) over `SlackClientOps` |
| `slack/renderer.py` | Slack reference `Renderer` (`SlackRenderer`) + `SlackApprovalDecider` + `build_approval_blocks` |
| `slack/transport_dispatch.py` | `handle_message_transport()` — full new-path dispatch wiring the three layers together |

## Layer 1 — `MessagingTransport` (`transport.py`)

Channel-neutral inbound/outbound contract. A new channel = implement this interface + an inbound adapter, with zero change to the shared turn-handling core.

- **Class attributes**: `channel_type: str` (e.g. `"slack"`) and a `capabilities: TransportCapabilities`.
- **Tier-1 core (abstract)**: `send_message(conversation_id, content, thread_id=None) -> str` (returns a platform message id), `resolve_conversation(user_id) -> str` (the `open_dm` equivalent), `fetch_history(conversation_id, thread_id=None) -> list[InboundMessage]`.
- **Lifecycle (default no-op, override as needed)**: `connect()` (lazy-import client libs HERE), `maintain()` (poll/heartbeat), `disconnect()`.
- **Inbound adapter (abstract)**: `receive(raw_envelope)` (ack → filter → authorize → normalize → dispatch) and `authorize(msg) -> bool`. `authorize` MUST be **deny-by-default** — an unconfigured transport authorizes nobody.

### `TransportCapabilities`

Declares what a channel can do. Defaults are deliberately conservative (the WhatsApp-like floor) so a transport that forgets to declare a capability degrades safely rather than over-promising.

| Field | Default | Notes |
|-------|---------|-------|
| `streaming` | `False` | feature flag |
| `edit` | `False` | feature flag |
| `reactions` | `False` | feature flag |
| `files` | `False` | feature flag |
| `rich_blocks` | `False` | feature flag |
| `threads` | `False` | feature flag |
| `max_message_chars` | `4096` | quantitative — Slack ~40000, Telegram 4096, Discord 2000, WhatsApp 4096 |
| `max_buttons` | `3` | interactive choices per prompt (WhatsApp reply buttons = 3) |
| `supports_proactive_send` | `True` | send-policy (WhatsApp: `False` outside its 24h window) |

`to_dict()` serializes all fields. The integer *parameters* (not booleans) capture where channels differ quantitatively so the `Renderer` can chunk / degrade rather than assume a single shape.

### `InboundMessage`

Normalized, channel-agnostic inbound message: `channel_type`, `user_id`, `conversation_id`, `text`, `thread_id=None`, `attachments=[]`, `is_mention=False`; `to_dict()` for serialization.

## Layer 2 — `TurnDriver` (`driver.py`)

Consumes a provider's `AcpEvent` stream and emits abstract `OutputEvent`s to a per-transport `Renderer`. It owns the channel-neutral turn concerns — credential/exfiltration redaction and the tool-approval decision — so every channel inherits them once.

**Redaction** — `_redact()` runs `redact_exfiltration_urls()` then `redact_credentials()` (both from `security.py`) over every text chunk, thinking chunk, tool title/purpose, and each string field of prompt-choice options before it reaches a renderer.

**`run(message) -> str`** — calls `renderer.on_turn_start()`, then translates each provider event into a dispatched `OutputEvent` and returns the accumulated (redacted) assistant text:

| Provider event | Emitted `OutputEvent` |
|----------------|-----------------------|
| `EVENT_TEXT_CHUNK` | `TEXT_CHUNK` (redacted, accumulated) |
| `EVENT_THINKING_CHUNK` | `THINKING` |
| `EVENT_TOOL_CALL` | `TOOL_CALL` (uniform — each call completes the prior task + starts a new one) |
| `EVENT_PERMISSION_REQUEST` | `PROMPT_CHOICE` (interactive w/ decider only) then approve/reject |
| `EVENT_COMPACTION_STATUS` | `COMPACTION` |
| `EVENT_COMPLETE` | `DONE` |

### Approval ladder

Four modes (constants, mirroring the native Slack + dashboard ladder):

| Constant | Value | Behavior in `_approve()` |
|----------|-------|--------------------------|
| `APPROVAL_AUTO` | `"auto"` | approve |
| `APPROVAL_TRUST` | `"trust"` | approve |
| `APPROVAL_TRUST_READS` | `"trust-reads"` | approve iff `event.tool_kind == "read"` |
| `APPROVAL_INTERACTIVE` | `"interactive"` | **deny-by-default** unless the injected `decider` approves |

Two injected predicates take precedence over the ladder (both checked per permission request, and both auto-approve immediately — no buttons, no decider wait):

- `auto_approve_tool: (tool_title) -> bool` — hook-driven auto-approve (e.g. `spawn_run` via the context builder's `auto_approve_subagent_spawn` hook). Reason logged as `hook_auto_approve`.
- `auto_approve_session: () -> bool` — honors per-session Trust / global YOLO without the driver importing any channel module. Reason logged as `session_trust`.

`decider: ApprovalDecider` (`Callable[[Any], Awaitable[bool]]`) supplies the interactive click; when omitted, interactive mode denies by default (so buttons are only rendered when a decider exists — otherwise the user would get dead controls). Every permission decision emits an `sel().log_api_access` event (`caller="turn_driver"`, `operation="tool_permission"`, `source="messaging"`, `outcome` one of `auto_approved` / `approved` / `denied`).

## Layer 2b — `Renderer` + `OutputEvent` (`renderer.py`)

### `OutputEvent`

Channel-neutral output event with a `kind` plus per-kind payload fields (`text`, `tool_call_id`, `title`, `tool_kind`, `tool_purpose`, `options`, `request_id`, `context_usage_pct`, `stop_reason`); `to_dict()` serializes them. Kinds: `TEXT_CHUNK`, `THINKING`, `TOOL_CALL`, `PROMPT_CHOICE`, `COMPACTION`, `DONE` — the full set is `OUTPUT_KINDS` (a `frozenset`). `prompt_choice` is a **first-class** event, not generic "permission text": each renderer maps it to its native interactive widget.

### `Renderer` ABC

Constructed with a `TransportCapabilities`. `dispatch(event)` routes each kind to the matching `on_*` handler and raises `ValueError` on an unknown kind. Handlers:

- `on_turn_start()` — default no-op, called once before the stream begins.
- `on_text_chunk(text)`, `on_thinking(text)` — abstract.
- `on_tool_call(tool_call_id, title, tool_kind="", tool_purpose="")` — abstract; mirrors native uniform tool-call semantics (each call marks the previous task complete and starts a new in-progress task).
- `on_prompt_choice(options, request_id)` — abstract; renders the interactive approval/choice prompt.
- `on_compaction(context_usage_pct)`, `on_done(stop_reason="")` — abstract.

### `chunk_text(text, max_chars) -> list[str]`

Pure helper Renderers use to honor `capabilities.max_message_chars`. Returns `[]` for empty input; a non-positive `max_chars` disables chunking (single chunk); otherwise splits into `max_chars`-sized pieces. Together with the `max_buttons` cap this is how a renderer *degrades* an over-cap message or choice set for a lower-capability channel.

## Layer 3 — session-key namespacing (`link.py`)

Session keys are namespaced as `f"{channel_type}:{conversation_id}"` (`session_key()`) so keys never collide across channels (`SLACK_NAMESPACE = "slack"`). Legacy native-Slack sessions were keyed by the bare `thread_ts`; helpers provide the bidirectional `bare ⇄ slack:` shim consumed by `SessionMap` (`session_map.py` imports `ChannelLink` + `canonical_key`, no import cycle):

- `is_legacy_slack_key(key)` — True iff `key` is a bare Slack `thread_ts` (matched by `_SLACK_TS_RE = r"\d+\.\d+"`, digits + one dot).
- `canonical_key(key)` — normalizes a bare legacy key to `slack:<thread>`; non-legacy keys (`dashboard:`, `channel:`, `slack:`, …) pass through unchanged. `SessionMap._load` (called from `__init__`) migrates bare keys and populates a Layer-3 `ChannelLink`; `get()`/`set()` re-canonicalize so a not-yet-updated caller passing a bare `thread_ts` still resolves.
- `legacy_key(key)` — returns the bare `thread_ts` for a `slack:<thread>` key, else `None`.

`ChannelLink(channel_type, channel_id=None, thread_id=None)` records the inbound channel a session belongs to (its **own** channel), with `to_dict()`/`from_dict()`. It is deliberately distinct from the dashboard→Slack *mirror* binding, which stays behind `SessionMap.get/set_slack_link` and is **not** modeled here (guardrail G3).

## Config flag & routing

`MessagingConfig.use_transport` (`config/loader.py`, default `True` in KiroCrew; exposed in `config.json` under `messaging`) is the single switch. `slack/events.py::_route_message` checks `orch._cfg.messaging.use_transport`; when `True` it creates a task on `handle_message_transport` and skips the native `handle_message` monolith. (There is no challenge-redirect in this fork — Slack messages are processed inline.) Approval mode is resolved by `_resolve_approval_mode(orch)` (respects configured mode + operator YOLO/SafetyOverride TTL), and the per-channel `slack.channels.<id>.agent` override is passed through.

## Telegram forum topics (per-Topic sessions)

A Telegram **supergroup with Topics enabled** maps onto the same `thread_id`
abstraction Slack uses, so one bot serves many parallel, topic-scoped sessions
(Slack channel+threads) instead of a single session per user.

- **Routing / session key.** The transport captures each update's
  `message_thread_id` (the Topic id) and carries it as the neutral
  `InboundMessage.thread_id`. The dispatcher folds `(chat_type, chat_id,
  thread_id)` into a route and reuses the `chat_type` slot of
  `build_dm_session_key`: a Topic keys to
  `telegram:{agent}:forum:{chat_id}:{thread_id}`, while a private DM stays
  byte-for-byte `telegram:{agent}:direct:{user_id}`. `messaging.dm_scope="unified"`
  collapses **only** direct DMs into the `unified:{agent}` bucket — forum routes
  always keep the full per-Topic key, so no group Topic can share a session with
  a DM or another group.
- **Per-Topic generation.** `ConversationState` is keyed on the same route, so
  `/new`, idle/daily rotation and `/compact` are scoped to one Topic.
- **Gate — fail-closed AND Topic-scoped.** `forum_gate_outcome(chat_type,
  chat_id, message_thread_id, *, allow_forum, allowed_forum_chat_ids)` is the
  single predicate guarding **both** `TelegramTransport.receive` (frozen
  allow-list) and `TelegramDispatcher.on_callback` (live cfg). It authorizes a
  turn/callback only for a real forum Topic — `chat_type == "supergroup"` AND a
  `message_thread_id` — in an allow-listed chat (`telegram.allow_forum` **and**
  `chat_id ∈ telegram.allowed_forum_chat_ids`). Ordinary groups and the
  supergroup **General** chat (no thread) are denied and SEL-audited
  (`denied_forum_not_allowed` / `denied_non_private_chat`); the owner
  `allowed_user_ids` check still gates *who* may drive a turn.
- **Outbound.** Streamed answers, command/notice replies, queue receipts, the
  queue drain, callback re-dispatch, and the `/link` dashboard-mirror
  `ChannelLink` all carry `message_thread_id`, so every reply lands in its Topic
  and a queued message drains under the forum key (`editMessageText` is not
  threaded — the message id already identifies the message within its Topic).

## Mid-turn routing & per-message overrides (Telegram)

A message arriving while a turn is in flight is routed by
`messaging.queue_mode` (default `steer`):

- **`steer`** — inject into the running turn via kiro-cli `_session/steer`.
  kiro-cli folds it at its next generation boundary and emits an inline
  `[STEERING steer-<id>: <ack summary>]` marker at the fold point. The user's
  steer message receives an emoji **reaction** (`setMessageReaction`;
  `TELEGRAM_CAPABILITIES.reactions=True`) as the delivery receipt.
- **`queue`** — hold the message; a single in-place "⏳ Queued (N)" receipt
  tracks the burst. When the turn ends, queued texts collapse into ONE combined
  follow-up turn (order preserved).

**Per-message overrides:** a `/steer <msg>` or `/queue <msg>` prefix forces
that message down the corresponding path, overriding `queue_mode` for that
message only. The prefix is only recognized when the original text is not
itself a command; the payload after the prefix is **turn content, never a
command** — `/queue /new` queues the literal text `/new`. Bare `/steer` /
`/queue` (no body) are treated as normal messages.

**Drain semantics:** the queue-drain replay calls `handle_message(...,
interpret_commands=False)`; drained payloads bypass both the command intercept
and override parsing, so queued command-lookalike text reaches the model as
literal content instead of executing on drain.

**Telegram rendering contract:** turns stream live via one real message edited
in place (throttled plaintext frames; transient `🔧 {tool}…` footer during tool
calls; trailing `[OPTIONS:]` markup held back from live frames). Segments seal
to Telegram-HTML at rotation points: each complete `[STEERING]` marker (the
pre-steer output seals; the continuation opens a fresh message headed by a
`↪️ <ack summary>` chip, lazily materialized only when real continuation text
follows — an end-of-stream marker posts no tail message) and length overflow
(fence-balanced via `_split_markdown`; a trailing incomplete directive is
detached before splitting). If sealing edits fail because the live message was
deleted, the final content is re-sent as a fresh message. See
`docs/mid-turn-queue-and-cancel.md` for the full behavioral walkthrough.

## Slack reference implementation

### `SlackTransport` (`slack/transport.py`)

Wraps `SlackClientOps` in the Layer-1 contract; declares Slack's real (rich-end) capabilities: `streaming/edit/reactions/files/rich_blocks/threads=True`, `max_message_chars=40000`, `max_buttons=5`. `authorize()` is **deny-by-default & owner-only** — an empty `allowed_users` frozenset (copied at construction so it can't mutate mid-decision) authorizes nobody, and every denial (including empty/missing `user_id`) is SEL-audited (`operation="slack_transport.authorize"`, `outcome="denied"`). `receive()` acks → drops bot-authored events (`bot_id` / `subtype == "bot_message"`) before authorization → normalizes to `InboundMessage` → authorizes → invokes the injected `dispatch` callback. The client is held **and exposed** via a `client` property (guardrail G2).

### `SlackRenderer` + `SlackApprovalDecider` (`slack/renderer.py`)

`SlackRenderer` maps the abstract `OutputEvent` stream onto Slack's streaming + Block Kit surface, reusing the native streaming machinery verbatim (bracket-hold `[OPTIONS:…]` filter, `_EDIT_INTERVAL` edit-throttle, `chat.update` cursor fallback when no streaming surface, `StatusReactionController` phase/emoji, per-tool task cards with a 30s elapsed timer, a timing footer at `on_done`). `on_turn_start` is idempotent (guarded by `_started`) so the dispatcher can fire the ack reaction early and the driver's later call no-ops.

`on_prompt_choice` renders `build_approval_blocks()` — three Block Kit buttons whose `action_id`s encode the request id:

| Button | `action_id` prefix | Scope |
|--------|--------------------|-------|
| Approve | `mc_tool_approve_` | this tool |
| Trust session | `mc_tool_trust_` | per-session auto-approve (not global YOLO) |
| Deny | `mc_tool_deny_` | this tool |

`SlackApprovalDecider` is the `TurnDriver` `decider`: `__call__` creates a per-request future (registered in a process-global `_REGISTRY` keyed by request id), awaits it with `asyncio.wait_for(..., timeout=_APPROVAL_TIMEOUT)`, and **denies by default** on timeout. The Slack interaction handler (`slack/interactions.py`) — which has no direct reference to the per-turn decider — resolves clicks via the classmethods `resolve_global(request_id, approved)` and `session_for(request_id)`; a Trust click calls `add_trusted_session()` before resolving so subsequent tools in the session are auto-approved (via the driver's `auto_approve_session` predicate).

### `handle_message_transport` (`slack/transport_dispatch.py`)

Full new-path dispatch: fires the ack reaction + working status immediately (constructing the `SlackRenderer` before the potentially slow session acquisition), acquires/creates the session, builds the message with context, then drives `TurnDriver.run()`. Agent resolution: thread override (`!agent`) → per-channel `agent_override` → configured default → the canonical `_DEFAULT_KIROCREW_AGENT = "kirocrew"` fallback (so the session loads kirocrew-core / `spawn_run` rather than kiro-cli's bare built-in default). It injects `auto_approve_tool=lambda title: _should_auto_approve_spawn(context_builder, title)` and `auto_approve_session=lambda: is_slack_session_trusted(session_key)`. Post-turn bookkeeping (context-usage accounting, conversation logging, success SEL audit) is each isolated in its own `try/except` so a bookkeeping failure never re-records a successful turn as a failure; `sessions.release()` runs in `finally`.

## Invariants

- **One-way dependency**: `kiro_crew.messaging` never imports `kiro_crew.slack` / `kiro_crew.dashboard`; violations reintroduce the cycle the abstraction removed.
- **Deny-by-default authorization**: `MessagingTransport.authorize` implementations authorize nobody when unconfigured; interactive approval denies unless positively approved (or a timeout elapses → deny).
- **Redaction is unconditional**: all LLM/tool-originated text flowing through `TurnDriver` passes `redact_exfiltration_urls()` + `redact_credentials()` before reaching any renderer.
- **Conservative capability defaults**: unspecified `TransportCapabilities` degrade safely (WhatsApp-like floor), and renderers must honor `max_message_chars` (`chunk_text`) and `max_buttons`.
- **Session keys are namespaced**: every key is `channel_type:conversation_id`; only bare legacy Slack `thread_ts` keys are shimmed, via `canonical_key`/`legacy_key`.
- **Own-channel vs. mirror**: `ChannelLink` models a session's own inbound channel only; the dashboard→Slack mirror binding stays in `SessionMap.get/set_slack_link` (guardrail G3). The generalized channel-neutral outbound mirror (`SessionMap.set_mirror_link`, PR #52) stores a `ChannelLink` under the `mirror` slot for non-Slack channels — still distinct from the session's own inbound link.

## Testing conventions

The extraction is gated by a **golden-transcript** harness (`test/test_slack_golden_transcript.py`): a `RecordingSlackClient` captures the ordered sequence of Slack-render operations the native `handle_message` emits for a scripted `ScriptedProvider` event stream, establishing the baseline the `TurnDriver` + `SlackRenderer` rewire must reproduce identically. Layer contracts and the Slack impl have dedicated suites: `test_messaging_transport.py`, `test_messaging_driver.py`, `test_slack_renderer.py`, `test_slack_transport.py`, `test_slack_transport_dispatch.py`, `test_slack_transport_integration.py`. Providers are always mocked (scripted event streams) — never spawn a real kiro-cli process.

## Slack settings API

Three dashboard-only endpoints back the `/settings?tab=slack` panel. They are
registered in the dashboard route block (NOT `_register_mcp_routes`, which is
also mounted on the token-less API-only server) so they always sit behind
dashboard token auth.

- `GET /api/slack/config` — masked token previews + presence booleans, owner
  ID, slash command, enterprise-org allowlist, behavior toggles, and live
  status: `connected` (recorded socket connect outcome), `connect_error`
  (short reason, e.g. `invalid_auth`), `read_only` (true unless the request
  is direct-local). Never returns a raw secret.
- `PUT /api/slack/config` — requires a direct-local request (loopback peer
  AND no `Forwarded`/`X-Forwarded-*`/`X-Real-IP` headers); remote gets 403.
  Validate-first/commit-last. New tokens are verified against Slack before
  storage (`auth.test` for bot, `apps.connections.open` for app tokens);
  rejection returns 400 and writes nothing, network failure saves with
  `verify_warning`. `<field>_clear` must be a strict boolean. Secrets land in
  `config_dir/.env` via atomic 0600 `mkstemp` + `os.replace`, and
  `os.environ` is synced afterward. Response `restart_required` is true for
  actual env changes and boot-read config (`command`,
  `allowed_enterprise_ids`); `reactions_enabled`/`show_thinking` apply live.
  An empty `command` resets the slash command to the default.
- `GET /api/slack/manifest` — public manifest template rendered with
  `?alias=` (default `kirocrew`, never `$USER`) plus Slack's one-click
  create deep link.

`allowed_users` / `open_channels` are intentionally not exposed while the
runtime enforces owner-only access.

## Discord channel

**Transport (`kiro_crew/discord/`).** A concrete `MessagingTransport` over a
pure-aiohttp Discord Gateway WebSocket client (`client.py`): identify with the
`DIRECT_MESSAGES` intent only, heartbeat at the server interval with jitter,
resume via `resume_gateway_url`/sequence tracking, exponential-backoff
reconnect, and hard stop on non-recoverable close codes (4004/4010-4014).
Outbound is REST v10 (send/edit/typing/reactions/interaction acks) with a
single 429 `retry_after` back-off; malformed (non-JSON) response bodies
degrade to an error result and never propagate into rendering. No public
webhook endpoint is required. `client.ready` (asyncio.Event) is set on
READY/RESUMED and cleared on disconnect; `maybe_start_discord` reports
`connected` only after `wait_ready` succeeds and keeps the dashboard badge
truthful via the `on_state_change` observer (a non-recoverable close flips it
back off with the reason).

**Security model.** `authorize` is deny-by-default against
`discord.allowed_user_ids` (snowflakes kept as strings — they exceed 2^53);
every denial is SEL-audited. DM-only fail-closed: any message carrying a
`guild_id` is rejected even from allow-listed users so tool output can never
land in a shared channel. Bot-authored messages (including our own) are
dropped in the client as a loop guard. `DISCORD_BOT_TOKEN` is on the sandbox
agent env denylist.

**Dispatch + rendering.** Turns ride the shared `TurnDriver`
(`transport_dispatch.py` mirrors the Telegram dispatcher: mid-turn
steer/queue with collapsing receipts, drain-collapse, `!compact` under atomic
`try_acquire`, dashboard mirror `!link`/`!unlink`). Text commands are
`!`-prefixed (`!new`, `!compact`, `!link`, `!unlink`, `!stop`, `!help`,
`!queue`/`!steer`; `/` aliases accepted) because Discord's client intercepts
bare `/` as slash-commands. The renderer streams via throttled in-place edits
under the 2000-char cap (chunked at 1900 with fence-balanced splitting),
rotates messages at `[STEERING]` markers with quote chips, renders trailing
`[OPTIONS:]` as button action rows (`opt:<i>`, label recovered from the
component at interaction time), and posts Approve/Deny buttons for
interactive tool approvals. Approval `custom_id`s carry a per-prompt random
nonce (`a:<request_id>:<nonce>:<1|0>`) validated at resolution — ACP request
IDs are reusable across provider/gateway restarts, so a stale button without
the matching nonce fails closed. The decision window denies by default on
timeout and retires the nonce with it.

## Discord settings API

- `GET /api/discord/config` — masked `bot_token_preview` + `bot_token_set`,
  `connected` (true only after the Gateway handshake reached READY this
  session), `connect_error`, `configured` (token AND enabled AND non-empty
  allowlist — the transport fails closed on an empty list), `read_only`
  (true unless the request is direct-local). Never returns a raw secret.
- `PUT /api/discord/config` — requires a direct-local request (loopback peer
  AND no forwarding headers); remote gets 403. Validate-first/commit-last.
  New tokens must match the three-segment bot-token shape (an accidental
  `Bot ` Authorization prefix or `DISCORD_BOT_TOKEN=` env line is stripped)
  and are verified against Discord `GET /users/@me` before storage; rejection
  returns 400 and writes nothing, network failure saves with
  `verify_warning`. `bot_token_clear` must be a strict boolean.
  `allowed_user_ids` accepts numeric snowflake strings only. Secrets land in
  `config_dir/.env` (atomic 0600) with `os.environ` synced; non-secrets go to
  `config.json` under `discord`. All fields are boot-read, so
  `restart_required` is true on any actual change.
## Telegram settings API

Two dashboard-only endpoints back the `/settings?tab=telegram` panel. Like the
Slack settings API they are registered in the dashboard route block (NOT
`_register_mcp_routes`) so they always sit behind dashboard token auth.

- `GET /api/telegram/config` — masked bot-token preview + presence boolean,
  `enabled` flag, `allowed_user_ids` (serialized as digit strings for the tag
  editor), `soft_threshold_pct`, and live status: `connected` (true only
  after startup proved the token with an authenticated `getMe` and the
  long-polling transport started; when Telegram is unreachable at boot the
  channel still starts and reports not-connected until the first successful
  poll — only a *rejected* token aborts startup and closes the client; the
  polling loop updates the flag live, deduped on state change — three
  consecutive `getUpdates` failures flip it false with a reason, the next
  success flips it back), `connect_error` (token-free short reason:
  `TelegramAuthError` message for a rejected token, exception class name
  otherwise), `read_only` (true unless the request is direct-local), and
  `configured` (token AND enabled AND non-empty allowlist — the transport
  fails closed and rejects every message while the allowlist is empty).
  Never returns a raw secret. Token presence considers both the
  `TELEGRAM_BOT_TOKEN` credential and the legacy `telegram.bot_token` config
  fallback.
- `PUT /api/telegram/config` — requires a direct-local request (same gate as
  the Slack save); remote gets 403. Validate-first/commit-last. Pasted tokens
  are shape-checked (`<bot_id>:<secret>`) and verified against Telegram
  `getMe` before storage; rejection returns 400 and writes nothing, network
  failure saves with `verify_warning`. `bot_token_clear` must be a strict
  boolean. The secret lands in `config_dir/.env` as `TELEGRAM_BOT_TOKEN` via
  the same atomic 0600 write, and `os.environ` is synced afterward. Setting
  OR clearing the token also purges the legacy `telegram.bot_token` field
  from `config.json` — the gateway falls back to that field when `.env` is
  empty, so leaving it behind would resurrect a removed credential on the
  next restart. `allowed_user_ids` accepts digit strings or ints and stores
  canonical deduplicated ints; `soft_threshold_pct` is an int in 1–100.
  Every Telegram field is boot-read (consumed in the orchestrator's
  constructor), so `restart_required` is true for any actual change and only
  for actual change.

## Webex channel

**Transport (`kiro_crew/webex/`).** A concrete `MessagingTransport` over a
pure-aiohttp Webex client (`client.py`): inbound rides a device-registration
WebSocket — the client registers a device with the Webex Device Management
service (WDM) to obtain a per-device WebSocket URL, connects, authorizes with
the bot token, and receives `conversation.activity` events (the same
mechanism the official `webex-bot` SDK uses; no public webhook endpoint is
required). **Caveat: WDM is an internal Cisco mechanism, not a documented
public API.** Cisco can change frame shapes or endpoints without notice, and
behavior may vary across geo/FedRAMP clusters (the client defaults to the
`wdm-a` host and the `us` Hydra cluster; both the WDM base and the REST base
are constructor parameters for containment). The documented alternative
(webhooks) requires a public inbound URL, which contradicts the local-first
design — this trade-off is deliberate. If WDM drifts, the failure mode is a
truthful "Not active" badge with the reconnect reason (the
`ready`/`on_state_change` machinery), never a silently green channel. A
manual live smoke test with a real bot token is a launch gate for this
channel. Activity events are treated purely as signals: the raw UUID is
Hydra-encoded (`base64("ciscospark://us/MESSAGE/{uuid}")`) and the message is
hydrated via the documented `GET /v1/messages/{id}` REST call in a background
task so the receive loop keeps breathing during long turns. Outbound is REST
(`POST/PUT/DELETE /v1/messages`) with a single 429 `Retry-After` back-off; an
email-shaped conversation id maps onto `toPersonEmail` (opens/reuses the 1:1
space server-side). Outbound markdown is bounded in UTF-8 BYTES, not
characters — Webex's limit is 7439 bytes. Final answers are split losslessly
into 7000-byte chunks (``chunk_utf8``, never splitting a code point) and
single sends are tail-guarded by ``truncate_utf8`` as a last resort, so a
multibyte-heavy reply is never rejected wholesale or silently truncated. The reconnect loop uses exponential backoff with a
minimum-healthy-connection guard so a bad token can never hot-loop.
``client.ready`` (asyncio.Event) is set on connect+authorize and cleared on
disconnect; ``maybe_start_webex`` reports ``connected`` only after
``wait_ready`` succeeds and keeps the dashboard badge truthful via the
``on_state_change`` observer (a disconnect flips it back off with the
reason).

**Security model.** `authorize` is deny-by-default against
`webex.allowed_emails` (lowercased comparison); every denial is SEL-audited.
Direct-rooms-only fail-closed: any message from a non-`direct` room is
rejected even from allow-listed users so tool output can never land in a
group space. Self-messages are dropped twice (WS actor email + hydrated
`personId` against the bot identity). `WEBEX_BOT_TOKEN` is on the sandbox
agent env denylist.

**Dispatch + rendering.** Turns ride the shared `TurnDriver`
(`transport_dispatch.py` mirrors the WeCom dispatcher: `/new`, `/compact`,
`/help` command intercept, mid-turn messages fold into the running turn via
steer gated on `has_active_turn`, `/compact` under atomic `try_acquire`,
soft/hard context-threshold notices as separate proactive messages). The
renderer is shaped by Webex's 10-edits-per-message cap: no typewriter
streaming (`streaming=False`); a "🤔 Thinking…" placeholder is posted at turn
start, tool-progress status edits are throttled and budgeted to 6 of the 10
edits (an edit failure burns the remaining budget so the final-answer edit
can never race the cap), and the final answer lands as one placeholder edit
with a fresh-message fallback plus chunked follow-ups past the 7000-char cap.
Trailing `[OPTIONS:]` markup is stripped (`max_buttons=0`); interactive tool
approvals run decider-less (deny-by-default under INTERACTIVE mode).

## Webex settings API

- `GET /api/webex/config` — masked `bot_token_preview` + `bot_token_set`,
  `connected` (true only while the device WebSocket is connected + authorized
  this session), `connect_error`, `configured` (token AND enabled AND non-empty
  allowlist — the transport fails closed on an empty list), `read_only`
  (true unless the request is direct-local). Never returns a raw secret.
- `PUT /api/webex/config` — requires a direct-local request (loopback peer
  AND no forwarding headers); remote gets 403. Validate-first/commit-last.
  New tokens (an accidental `WEBEX_BOT_TOKEN=` env line is stripped) are
  verified against Webex `GET /v1/people/me` before storage; rejection
  returns 400 and writes nothing, network failure saves with
  `verify_warning`. `bot_token_clear` must be a strict boolean.
  `allowed_emails` accepts syntactically valid emails only. Secrets land in
  `config_dir/.env` (atomic 0600) with `os.environ` synced; non-secrets go
  to `config.json` under `webex`, and any token set/clear purges the legacy
  `webex.bot_token` config fallback (config.json commits before .env so a
  crash between the two cannot resurrect the plaintext copy). Writes are
  serialized under the repo-wide config lock. All fields are boot-read, so
  `restart_required` is true on any actual change.
