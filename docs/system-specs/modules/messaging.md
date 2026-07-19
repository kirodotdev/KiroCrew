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
