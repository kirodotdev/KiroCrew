# Messaging Transport Module

## Overview

`kiro_crew.messaging` is the channel-neutral transport abstraction used by the shipped Slack, Discord, Telegram, Webex, WeCom, Microsoft Teams, Weixin, and Feishu integrations; its conservative contract also leaves room for future channels such as WhatsApp. It avoids re-implementing streaming, tool approval, session identity, or rendering for each integration. It holds the channel-neutral core of the Slack turn loop (`slack/handler.py::handle_message`) so a new channel implements only two small interfaces (a `MessagingTransport` + a `Renderer`) and inherits everything else.

**Dependency direction is one-way:** `slack` / `dashboard` → `messaging`, never the reverse. The `kiro_crew.messaging` package imports nothing from `kiro_crew.slack` or `kiro_crew.dashboard`; its only first-party dependencies are the shared lower-level helpers — `acp.types` event constants, the `security` redactors (`redact_credentials` / `redact_exfiltration_urls`), and `sel` for audit.

Slack's transport path is gated behind the `messaging.use_transport` config flag (default `true` in Kiro Crew, so the abstraction is the canonical path); when off, Slack's native `handle_message` path runs instead.

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
| `messaging/transport.py` | **Layer 1** — `MessagingTransport` ABC + the `TransportCapabilities`, `InboundMessage`, and `ConfiguredChannelTarget` value objects (stdlib-only) |
| `messaging/driver.py` | **Layer 2** — `TurnDriver` (channel-neutral turn loop), approval-mode constants, `_redact` helper |
| `messaging/renderer.py` | **Layer 2b** — `Renderer` ABC, `OutputEvent`, output-kind constants + `OUTPUT_KINDS`, `chunk_text` helper, `apply_options_cap`/`cap_choices`/`format_overflow` (`max_buttons` enforcement) |
| `messaging/display_safety.py` | `strip_ansi` / `canonicalize_display` / `redact_for_display` — credential redaction against the form a platform RENDERS, not the bytes sent. Hoisted out of `slack/format.py` when the shared overflow sink began writing choice text into the parsed body on every widget channel |
| `messaging/split.py` | `split_markdown_safe` — the shared fence-safe markdown splitter (stdlib-only, pure). Prefix-stable so streaming callers can send sealed chunks and keep only the last as a live buffer |
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
| `max_buttons` | `3` | TOTAL interactive choices per prompt (WhatsApp reply buttons = 3); enforced via `apply_options_cap` — overflow degrades to a numbered text list |
| `supports_proactive_send` | `True` | send-policy (WhatsApp: `False` outside its 24h window) |

`to_dict()` serializes all fields. The integer *parameters* (not booleans) capture where channels differ quantitatively so the `Renderer` can chunk / degrade rather than assume a single shape.

### `InboundMessage`

Normalized, channel-agnostic inbound message: `channel_type`, `user_id`, `conversation_id`, `text`, `thread_id=None`, `attachments=[]`, `is_mention=False`; `to_dict()` for serialization.

## Layer 2 — `TurnDriver` (`driver.py`)

Consumes a provider's `AcpEvent` stream and emits abstract `OutputEvent`s to a per-transport `Renderer`. It owns the channel-neutral turn concerns — credential/exfiltration redaction and the tool-approval decision — so every channel inherits them once.

**Redaction and protocol framing** — before text reaches a renderer, `TurnDriver` first classifies a reserved summary-bearing compaction notice at the start of the turn, then incrementally parses kiro-cli's inline `[STEERING steer-<id>: …]` frame across arbitrary chunk boundaries. Compaction summary bodies become the terse `✅ Context compacted.` receipt. Steering frames never become text: they emit one structured `STEER_CONSUMED` event at the exact boundary (paired with kiro-cli's typed lifecycle event regardless of arrival order). The user-facing `[OPTIONS: …]` trailer is deliberately not part of this filter and passes through unchanged for renderer-native buttons. After framing, `_redact()` runs `redact_exfiltration_urls()` then `redact_credentials()` (both from `security.py`) over every text chunk, thinking chunk, tool title/purpose, and each string field of prompt-choice options before it reaches a renderer.

The dashboard does **not** flow through `TurnDriver`; it remains unchanged as the authoritative transcript surface. Direct channel paths that bypass the driver are sanitized at source: Discord's explicit five-message resume replay strips legacy steering frames and summary-bearing compaction notices, while direct compact commands publish only terse receipts. Stored transcripts remain intact for audit.

**`run(message) -> str`** — calls `renderer.on_turn_start()`, then translates each provider event into a dispatched `OutputEvent` and returns the accumulated (redacted) assistant text:

| Provider event | Emitted `OutputEvent` |
|----------------|-----------------------|
| `EVENT_TEXT_CHUNK` | `TEXT_CHUNK` (protocol-framed, redacted, accumulated); inline steering frames become `STEER_CONSUMED`, compaction summary notices become a terse receipt |
| `EVENT_THINKING_CHUNK` | `THINKING` |
| `EVENT_STEER_CONSUMED` | paired with the inline frame so exactly one `STEER_CONSUMED` boundary reaches the renderer |
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
- `on_steer_consumed(summary="")` — default no-op; Discord/Telegram seal the pre-steer segment and open the continuation with a native acknowledgement chip using the parsed summary, without receiving raw protocol text.

### `SilentRenderer` — enforcing a dashboard channel disconnect

A `Renderer` whose handlers are all no-ops, substituted for the real renderer when
the conversation has been **disconnected** in the dashboard (see the pause markers
in [session](session.md)). Disconnect means "stop talking to me there": the turn
STILL RUNS and the inbound message still lands in the session, because the binding
is retained and the dashboard is where that user is now working — only the writes
back to the muted conversation are dropped, including the typing indicator.

`dispatch.delivery_is_muted(sessions, session_key, channel_type)` is the single
predicate; `conversation_is_muted(sessions, turn)` delegates to it for the shared
pipeline. It resolves origin-vs-mirror from the turn itself (a channel-born
session's key IS its conversation, so a turn arriving in that namespace is the
origin; anything else came over a mirror/resume binding) and fails OPEN, matching
the dashboard-side predicates — a muted conversation that stays noisy is a visible
bug, a live conversation silently dead is worse.

**Every inbound pipeline must consult it.** `drive_turn` does, but Discord and
Telegram run their OWN copies of the turn loop, so each substitutes independently;
a new channel that skips this ships a dashboard control with nothing behind it.
Two contracts matter for the substitute: its `close` accepts `*args/**kwargs`
because a channel may WIDEN that signature (Telegram passes `failure_reason`), and
it must be the object that is **closed**, not merely the one streamed to — a
concrete renderer's `close` posts an error placeholder when a turn produced no
output, which a muted turn always did. It is also deliberately NOT published into a
dispatcher's `_active_renderers`, which silences the mid-turn steer chip and keeps
channel-local APIs (`note_steer`) off the shared class. Slack never reaches these
pipelines — it drives its own gateway and is gated by `slack_mirror_is_paused`.

### `chunk_text(text, max_chars) -> list[str]`

Pure helper Renderers use to honor `capabilities.max_message_chars`. Returns `[]` for empty input; a non-positive `max_chars` disables chunking (single chunk); otherwise splits into `max_chars`-sized pieces. Together with the `max_buttons` cap this is how a renderer *degrades* an over-cap message or choice set for a lower-capability channel.

## Fence-safe splitting (`split.py`)

`split_markdown_safe(text, limit, *, reserve=0) -> list[str]` is the shared markdown splitter every channel converges on. `chunk_text` above is blind fixed-width and the per-channel splitters (Discord's and Telegram's `_split_text`/`_split_markdown`, `slack/format.py::split_message`, the Webex and Weixin helpers) each carry their own fence handling, so a fix landed in one never reached the others. The module is stdlib-only and pure — no config objects, no modes.

Its contract:

- **Budget.** Every chunk is at most `limit - reserve` characters; `reserve` holds back capacity for a suffix the caller appends to each chunk. Empty text → `[]`. Text that already fits, a non-positive `limit`, and a `reserve` that consumes the whole budget → `[text]`. Lengths are Python `str` characters, not bytes or UTF-16 units. One documented exception: a logical line that admits no cut clean on both sides is placed **whole** rather than cut into a fabricated fence delimiter, whenever the **line itself** is no longer than `limit` — such a chunk holds that one line and nothing else. Eligibility measures the line alone, so the chunk adds its fence scaffolding (the reopener line, and the newline plus synthetic closer) on top and may pass `limit` by exactly that scaffolding; a chunk with no scaffolding to carry stays within `limit`. A bounded oversize is the accepted price of never fabricating a delimiter. Which placement a line takes is decided from the line and `limit` alone, before any budget arithmetic (remaining room, the reserved closer, what the chunk already holds) gets a say, so a cut without a clean boundary is reachable only for a line longer than `limit` — eligibility written as a guard along one arithmetic path is what made the ladder bypassable at budgets a fence's scaffolding consumes whole.
- **Real fence grammar.** An opener is ≤3 spaces of indent plus a run of ≥3 backticks or tildes (a backtick fence's info string may not contain a backtick); a closer is a run of the SAME character at least as long with nothing else on the line. Fence content is **opaque**, so a ``` line inside a ````diff block is content — the backtick-parity counters in the per-channel splitters get exactly this wrong and invert their open/closed state for the rest of the message.
- **Language-tag carry.** A cut inside a fence seals the chunk with a synthetic closer (same char, matching length) and reopens the next chunk with the original opener line, info string and indent included. The closer's cost is budgeted while inside a fence, so sealing can never push a chunk over — except after a whole-line placement, which deliberately does not reserve it (see Budget above).
- **Prefix stability (the streaming contract).** Splitting is greedy left-to-right and every cut depends only on the text BEFORE it, so re-splitting a longer prefix of the same stream reproduces every chunk except the last one byte-for-byte. A streaming caller sends sealed chunks as they appear and keeps only the final chunk as a live buffer. This outranks cut-quality heuristics: a nicer cut point that has to peek at the line after the cut is not allowed.
- **Cut preference.** Outside a fence: paragraph break if one sits at least halfway into the budget, else the last line break if it sits at least a quarter in, else a hard cut filling the budget (Discord's `limit//2` / `limit//4` ladder). Inside a fence: line boundaries only, hard-cutting a line only when it cannot fit a chunk at all. A hard cut splits one line across a chunk boundary, so **both** halves start a rendered line and the cut is pulled back until neither invents a fence: the prefix must leave the fence state untouched, and the remainder must not begin with indent or a fence character (judged by its first character alone — parsing a remainder that is still arriving would move an already-sealed cut). A fragment where every candidate lands inside a run admits no such cut, and the residue policy has two tiers: the line is placed **whole**, in a chunk holding it and nothing else, whenever the **line itself** is no longer than the caller's full `limit` — measured on the line alone, so the chunk carries its fence scaffolding on top of `limit` rather than cutting the line to make room (see Budget above); only a logical line longer than `limit`, undeliverable whole at any budget the caller allows, falls back to the widest prefix-clean cut, where the deferred remainder can still read as a delimiter. That last case is documented degradation in the same regime as an under-sized budget, and it now requires a single unbreakable line longer than the whole limit — not merely a no-clean-cut fragment that outgrew the current chunk, and not one whose fence scaffolding pushed the scaffolded sum over. The whole-line placement is reached by sealing the buffered chunk first, and that seal is keyed on cut cleanliness alone, never on the line's length: a seal driven by a line still arriving would move once the rest of it landed, rewriting a chunk already sent.
- **Whitespace.** Leading whitespace is never stripped (stripping it silently re-indents split code). Trailing whitespace is trimmed only when sealing outside a fence, where it cannot be content.
- **Tables.** A trailing pipe-bearing line is pushed to the next chunk when an earlier cut is nearby, which keeps a header row with its separator row; otherwise table lines are plain lines. Full table conversion stays with the per-channel renderers.
- **Termination.** Pathological input — a single unbreakable 10k-char line, a 5000-backtick run, a budget too small to hold a line's own fence scaffolding — terminates, at worst emitting over-budget chunks rather than spinning. Whole-line placement seals progress by consuming the line; the dirty-cut fallback keeps a width of at least one character. The **final** chunk of an unclosed fence is left open on purpose: callers own final presentation, and a streaming caller still holds it as a live buffer.

No call sites yet: the channels route onto it in follow-up changes, and `test/test_messaging_split.py` pins each contract item above.

## Layer 3 — session-key namespacing (`link.py`)

Session keys are namespaced as `f"{channel_type}:{conversation_id}"` (`session_key()`) so keys never collide across channels (`SLACK_NAMESPACE = "slack"`). Legacy native-Slack sessions were keyed by the bare `thread_ts`; helpers provide the bidirectional `bare ⇄ slack:` shim consumed by `SessionMap` (`session_map.py` imports `ChannelLink` + `canonical_key`, no import cycle):

- `is_legacy_slack_key(key)` — True iff `key` is a bare Slack `thread_ts` (matched by `_SLACK_TS_RE = r"\d+\.\d+"`, digits + one dot).
- `canonical_key(key)` — normalizes a bare legacy key to `slack:<thread>`; non-legacy keys (`dashboard:`, `channel:`, `slack:`, …) pass through unchanged. `SessionMap._load` (called from `__init__`) migrates bare keys and populates a Layer-3 `ChannelLink`; `get()`/`set()` re-canonicalize so a not-yet-updated caller passing a bare `thread_ts` still resolves.
- `legacy_key(key)` — returns the bare `thread_ts` for a `slack:<thread>` key, else `None`.

`ChannelLink(channel_type, channel_id=None, thread_id=None)` records the inbound channel a session belongs to (its **own** channel), with `to_dict()`/`from_dict()`. It is deliberately distinct from the dashboard→Slack *mirror* binding, which stays behind `SessionMap.get/set_slack_link` and is **not** modeled here (guardrail G3).

## Config flag & routing

`MessagingConfig.use_transport` (`config/loader.py`, default `True` in Kiro Crew; exposed in `config.json` under `messaging`) is the single switch. `slack/events.py::_route_message` checks `orch._cfg.messaging.use_transport`; when `True` it creates a task on `handle_message_transport` and skips the native `handle_message` monolith. (There is no challenge-redirect in this fork — Slack messages are processed inline.) Approval mode is resolved by `_resolve_approval_mode(orch)` (respects configured mode + operator YOLO/SafetyOverride TTL), and the per-channel `slack.channels.<id>.agent` override is passed through.

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

## Mid-turn routing, queue receipts & cancel

A message that arrives while a turn is still generating is not a new turn: the
session semaphore is held, so running it directly would either block or open a
second conversation against the same key. Two channels carry the full
steer/queue/drain machinery, `telegram/transport_dispatch.py` and
`discord/transport_dispatch.py`; both read the same
`messaging.queue_mode` (`config/loader.py`, `"steer"` | `"queue"`, anything
else normalized to `steer`) and both implement the same three primitives
(`_handle_busy`, `_enqueue_with_receipt` + `_drain_queue`, `_handle_stop`).

The **channel-neutral half of the queue receipt is shared**, not duplicated:
`messaging/queue_receipt.py` owns the receipt registry, the lock, the three
lifecycle transitions and the receipt body formatting. Each channel reaches it
through a `ReceiptSurface` whose address is bound at construction, which is why
the shared module never sees a `chat_id` / `channel_id` / forum thread and
Telegram's forum routing stays entirely channel-local. `_handle_busy` and
`_drain_queue` deliberately stay per-channel: they re-enter their own
`handle_message` and own the per-channel `_active_renderers`.

The remaining channels (Webex, WeCom, Teams, Weixin, Feishu) implement `_handle_busy`
as **steer-only**: they fold the message into the running turn and reply with a
one-shot notice, or ask the user to resend when steer is unavailable. They have
no receipt and no drain, because their reply is bound to the inbound request
(WeCom, Weixin) or has no editable-receipt affordance, so a hold-then-deliver
follow-up turn could not be delivered reliably later.

### `steer` (the default): fold into the running turn

`_handle_busy` injects the text into the in-flight turn via kiro-cli's
`_session/steer` ext-method. The write is fire-and-forget: the turn's read loop
is the single consumer of that process's stdout, so awaiting the response would
steal the turn's own messages. kiro-cli folds the steer at its next generation
boundary (a tool-call edge on an agentic turn, the end of stream on one long
text turn) and emits an inline `[STEERING steer-<id>: <ack summary>]` marker in
the text stream at the exact fold point.

Two preconditions gate the steer, and both matter:

- `provider.supports_steer` — membership in `ACP_BACKENDS_STEER`, since the
  dormant Claude backend seam has no `_session/steer`. When false the message
  falls through to the queue path.
- `provider.has_active_turn()`, **not** `sessions.is_busy()`. `is_busy` stays
  true through post-turn bookkeeping (success record, turn persist, threshold
  notice, SEL audit, all await points), so it alone cannot distinguish a live
  turn from one that just ended. Steering an already-ended prompt is silently
  swallowed, which would leave the user with an acknowledgement and no answer.

On a successful steer the user's own message gets an emoji **reaction** as the
delivery receipt (`setMessageReaction` on Telegram, `add_reaction` on Discord;
both declare `reactions=True` in their `TransportCapabilities`). A reaction and
not a reply, so a mid-turn steer costs no extra bubble in the transcript. The
dispatcher also records the user's own words on the live renderer via
`note_steer` so the rendered chip quotes the user rather than the redacted
backend echo.

Attachments force the queue path on Discord: `_session/steer` carries text only,
so a mid-turn message with files would lose them.

### `queue`: one collapsing receipt, then ONE combined turn

In `queue` mode (or under a per-message override, or when steer is unavailable)
the message is held and surfaced through a **single** receipt message that grows
in place:

```
⏳ Queued (2): "what time is it" · "and the weather?"
```

The first five items are listed verbatim (`RECEIPT_MAX_ITEMS`), the rest
collapse into `…and N more` so a large burst cannot blow the message cap.

**The receipt is EDITED, never deleted.** At the end of the turn it flips to
`▶️ Now answering (N): …`; a `/stop` finalizes it to `🛑 Cancelled (N): …`.
Neither dispatcher calls a delete API on it. This is deliberate: the receipt is
the durable record of what the user asked and how it was routed, so deleting it
would erase the only evidence that a message was accepted at all.

The enqueue and the receipt create/grow happen together under
`ReceiptQueue.lock`, which the end-of-turn drain also takes across its dequeue
plus flip. The lock is deliberately **caller-held** rather than acquired inside
each transition (hence the `_locked` suffixes): moving the acquire inside would
read tidier and silently reintroduce the orphaned-bubble race. That is
what makes the two race-free: the drain sees either the message queued **with**
its receipt or neither yet, never a half state that would orphan a bubble.
`enqueue(..., force=False)` is a no-op once the semaphore is free, so if the
turn finished inside the window the enqueue returns false and the caller runs
the message as a fresh turn instead of stranding it.

**Queued messages collapse into ONE turn.** `_drain_queue` dequeues the whole
burst, joins the texts with blank lines in arrival order, and runs a single
combined turn, rather than replaying N separate turns. Two caps bound the
collapse: `_MAX_COLLAPSE` (50) messages, and on Discord the ingest attachment
limit across the combined set. Once one item no longer fits, it **and everything
behind it** are re-enqueued so FIFO order stays exact, the receipt notes
`+N deferred`, and the drain loops to pump the remainder. Messages arriving
during the combined turn open a fresh receipt and drain after it.

The combined turn itself runs outside `ReceiptQueue.lock`, and the drain replays via
`handle_message(..., interpret_commands=False)`. Drained payloads therefore
bypass both the command intercept and override parsing, so a queued `/new`
reaches the model as literal text instead of executing on drain.

### Per-message overrides

A `steer` / `queue` directive prefix forces that one message down the
corresponding path, overriding `queue_mode` for that message only.
**Discord's text commands are `!`-prefixed** (`!new`, `!compact`, `!link`,
`!unlink`, `!stop`, `!help`, `!sessions`, `!queue`, `!steer`) because Discord's
client swallows a bare `/` message into its own slash-command UI; the `/` forms
are also accepted for muscle-memory parity with Telegram, which uses `/` only.

The prefix is recognized only when the original text is not itself a command,
and the payload after it is **turn content, never a command**: `/queue /new`
queues the literal text `/new`.

A bare `/steer` or `/queue` carrying no message body matches neither the command
parser nor the override parser. **Telegram** answers it with the directive's
usage, because the alternative is handing the literal string `/queue` to the
model, which then answers it as chat text — indistinguishable, to the user, from
the feature not existing. **Discord** still treats the bare token as an ordinary
message; the two channels therefore diverge on this one case until the guard is
ported.

### Hard cancel: `/stop`

`/stop` (alias `/cancel`; `!stop` / `!cancel` on Discord) aborts the running
turn, drops every queued message, and finalizes the receipt to `🛑 Cancelled`.
`clear_queue` and the receipt finalize run together under `ReceiptQueue.lock`.

**Cancel is cooperative before it is fatal.** The dispatchers call
`provider.cancel(wait_ack_timeout=0)`, which writes an ACP `session/cancel`
notification and returns without waiting, so the acknowledgement to the user is
immediate; the turn stops at its next safe point. Per the ACP spec the ack is
not a response to that notification, it arrives as `stopReason: "cancelled"` on
the `session/prompt` response. The client arms a cancel grace window
(`_CANCEL_GRACE_SECS`, 10s floor, raised to the caller's budget when larger) and
only treats the agent as unresponsive after it elapses. The dashboard and Slack
Stop paths go through `SessionManager.stop_turn`, which waits out
`agent.soft_stop_budget_secs` (default 10.0, clamped to [0.5, 60]) for that ack
and escalates to a hard kill plus eager respawn only on timeout or error. See
`../../architecture/design-notes/soft-stop.md`.

On a shared runtime the cooperative cancel cannot force-kill a co-tenant
process, which is why the soft path exists at all rather than always killing.

### Streaming and steer rotation in the renderers

Both renderers stream a turn live through one real message edited in place
(throttled frames, a transient `🔧 {tool}…` footer during tool calls, trailing
`[OPTIONS:]` markup held back from live frames), and rotate to a new message at
the driver's structured steer boundary. Telegram seals segments to Telegram-HTML
and caps source at 4000 chars; Discord sends markdown as-is and caps at 1900,
under the platform's 2000 hard limit.

At a rotation the pre-steer output **seals** as its own message and the
continuation opens a fresh message headed by a chip quoting the marker's ack
summary (falling back to the user's own steer text recorded by `note_steer`):

```
> ↪️ answered the weather question in parallel with the directory summary
<steered continuation…>
```

**The chip is lazily materialized.** `_materialize_chip` prepends it only once
real post-steer text exists in the segment, so a marker at the very end of the
stream (the steer was already covered by the answer) posts **no tail message at
all** and the reaction remains the only acknowledgement. Without the laziness
every trailing steer would leave a chip-only bubble carrying no content.

A trailing `[OPTIONS:]` block belongs to the visible pre-steer answer, so it is
extracted before the seal and shipped as a keyboard on the sealed message,
rather than frozen as literal protocol text the user cannot act on. Length
overflow rotates too, fence-balanced so a code block spanning the cut is closed
at the seal and reopened after it, with a trailing incomplete directive detached
before the split. Raw markers never reach posted text; each renderer keeps a
defensive raw-marker parser only for callers that bypass `TurnDriver`.

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
- **Protocol metadata is not assistant speech**: streamed steering frames are withheld until complete, removed even when split across chunks, and represented as a structured boundary. Summary-bearing compaction activity is never sent to a channel as assistant speech; only a terse receipt may be rendered. `[OPTIONS: …]` remains user-facing and is never stripped by the shared filter.
- **Conservative capability defaults**: unspecified `TransportCapabilities` degrade safely (WhatsApp-like floor), and renderers must honor `max_message_chars` (`chunk_text`) and `max_buttons`.
- **A media-only inbound message is a message**: a transport whose text extraction comes back empty may only drop the envelope when there are also no media items. Weixin previously returned early on empty text, so an uncaptioned screenshot was discarded with no reply and no log line — the sender saw a successful send while the agent was never told anything arrived. Emptiness is a reason to drop only when the whole envelope is empty.
- **Weixin inbound media is CDN-indirect**: iLink envelopes never carry bytes, only a `CDNMedia` reference (`encrypt_query_param` + `aes_key`) whose object is AES-128-ECB encrypted on the WeChat CDN. `weixin/media.py` owns that protocol work (URL construction with percent-encoded params, key decoding, decrypt, a streaming size cap enforced on bytes read rather than `Content-Length`); `weixin/attachments.py` maps the four CDN-backed item types onto the shared `Attachment` and hands them to `messaging/attachments.py`, which keeps classification, limits, signature validation and temp-file ownership channel-neutral. The `aes_key` field carries **two** encodings for the same value — `base64(raw 16 bytes)` for images, `base64(ascii hex)` for file/voice/video — discriminated by decoded length plus a strict hex check, because guessing wrong yields plausible garbage rather than an error. A voice item that already carries server-side `text` short-circuits the download: iLink voice is SILK, which no shipped transcription backend decodes, so the local path is strictly worse than the transcript the server gave us. `files_inbound=True` reflects this; `files_outbound` stays `False` until the `getuploadurl` + encrypted CDN PUT half lands.
- **A mid-turn queue receipt is edited, never deleted**: it flips in place to `▶️ Now answering` on drain and to `🛑 Cancelled` on `/stop`. It is the durable record that a held message was accepted, so no path may delete it.
- **A queued burst drains as ONE turn**: `_drain_queue` joins the held texts in arrival order into a single combined turn (capped by `_MAX_COLLAPSE` and, on Discord, the attachment ingest limit), never N replayed turns. Anything past a cap is re-enqueued together with everything behind it so FIFO order stays exact.
- **A mid-turn steer requires a genuinely live turn**: gate on `provider.has_active_turn()`, never on `sessions.is_busy()` alone, which stays true through post-turn bookkeeping. Steering an ended prompt is silently swallowed, producing an acknowledgement with no answer.
- **Cancel is cooperative before it is fatal**: `/stop` sends the ACP `session/cancel` notification and lets the turn stop at its next safe point; escalation to a hard kill happens only after the soft-stop budget elapses without an ack. On a shared runtime the cooperative path is the only one that cannot take a co-tenant down with it.
- **Session keys are namespaced**: every key is `channel_type:conversation_id`; only bare legacy Slack `thread_ts` keys are shimmed, via `canonical_key`/`legacy_key`.
- **Runtime identity follows the current turn**: every channel dispatcher passes its trusted transport name as `runtime_source` to `ContextBuilder.build_message`; the shared `drive_turn` pipeline uses `ChannelTurn.channel_type`. A cross-surface resume keeps its original stable session key for conversation continuity, but `[RUNTIME]` names the interface carrying the current message. Follow-up turns refresh the marker because the one-time session context may describe an earlier surface.
- **Channel dashboard visibility is immediate**: after the first successful turn of a Discord, Telegram, Webex, Teams, WeCom, Weixin, or Feishu-owned session is persisted, the dispatcher triggers the channel-slot reconciler immediately when `dashboard.surface_channel_sessions` is enabled. `DashboardState.register_channel_transport` injects the dashboard state into the bound dispatcher; the lifetime 30-second reconciler remains the recovery path, but the normal first-turn path does not wait for it. Turns that resume an existing `dashboard:` session skip this step because that session already owns a slot.
- **Configured outbound targets are transport-owned**: `MessagingTransport.configured_targets()` returns opaque `ConfiguredChannelTarget` records for the user-configured destinations a dashboard session may link to, including an explicit unavailable reason when a protocol needs prior inbound state or cannot send proactively. `resolve_configured_target()` revalidates the selected opaque id at the side-effect boundary and resolves it to `(conversation_id, thread_id)`; the browser never supplies an unchecked platform conversation id. Discord exposes configured users and threads, and fail-closes thread resolution unless Discord still reports the allow-listed id as an actual thread rather than a normal shared guild channel; Telegram and Webex expose configured DMs; Weixin exposes allow-listed DMs plus authorized peers learned under its open policy; Teams destinations become available after an authorized inbound activity supplies a conversation/service URL; and WeCom destinations (including its allow-all policy placeholder) remain visible but unavailable because its reply token is inbound-bound; Feishu destinations are visible but unavailable because replies are anchored to an inbound message (no proactive DM in v1).
- **Configured-target egress is governed at every yield boundary**: the dashboard mirror-link endpoint enters the shared fail-closed `channels` governance ladder before resolving an opaque target (resolution may itself open a remote DM), rechecks before the initial link message, and rechecks before each historical-context message. A profile that narrows after transport startup therefore stops both target resolution and all subsequent sends.
- **Own-channel vs. mirror**: `ChannelLink` models a session's own inbound channel only; the dashboard→Slack mirror binding stays in `SessionMap.get/set_slack_link` (guardrail G3). The generalized channel-neutral outbound mirror (`SessionMap.set_mirror_link`) stores a `ChannelLink` under the `mirror` slot for non-Slack channels, still distinct from the session's own inbound link.
- **Managed-MCP session-key resolution**: every turn-running surface publishes `session_pid_<pid>.txt` (with an HMAC-SHA256 sidecar) through the single shared helper `messaging.identity.publish_turn_identity` (which calls `session_pid_sig.publish_session_pid`), keyed by the session's kiro-cli host PID, so the gateway's ancestor PID-walk resolves the caller's `X-Session-Key`. One writer is called by the dashboard, native Slack, and every shipped channel transport-dispatch surface: Telegram (DM + forum), Discord, Slack, Webex, WeCom, Teams, Weixin, and Feishu (through the shared `drive_turn`). Any surface that omits it makes every session-keyed managed MCP tool (`learn_add`, cron management, …) fail with HTTP 400 `missing X-Session-Key` from that channel's turns; the identity-topology test guards every dispatcher against regressing.

## Testing conventions

The extraction is gated by a **golden-transcript** harness (`test/test_slack_golden_transcript.py`): a `RecordingSlackClient` captures the ordered sequence of Slack-render operations the native `handle_message` emits for a scripted `ScriptedProvider` event stream, establishing the baseline the `TurnDriver` + `SlackRenderer` rewire must reproduce identically. Layer contracts and the Slack impl have dedicated suites: `test_messaging_transport.py`, `test_messaging_driver.py`, `test_slack_renderer.py`, `test_slack_transport.py`, `test_slack_transport_dispatch.py`, `test_slack_transport_integration.py`. Providers are always mocked (scripted event streams) — never spawn a real kiro-cli process.

## Slack settings API

Three dashboard-only endpoints back the `/settings?tab=channels&channel=slack` panel (legacy `?tab=slack` links redirect there). They are
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
pure-aiohttp Discord Gateway WebSocket client (`client.py`): identify with
`DIRECT_MESSAGES` for DM-only installs; when `allowed_thread_ids` is non-empty,
also request `GUILD_MESSAGES` and privileged `MESSAGE_CONTENT`. Heartbeat uses
the server interval with jitter,
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
`discord.allowed_user_ids` (snowflakes kept as strings — they exceed 2^53).
DM denials and authorization failures in configured threads are SEL-audited.
Because Discord's global guild/message-content intents deliver every visible
channel message, unrelated guild chatter is discarded silently; an approved
user attempting an unapproved thread remains audit-worthy. Guild turns require
both an approved sender and an exact `discord.allowed_thread_ids` match, then a
REST channel lookup must confirm Discord type 10/11/12 before dispatch. Normal
guild channels are always rejected. An approved thread is a shared disclosure
boundary: every member who can view it can read agent/tool output. Enabling any
thread also means Discord delivers message content from every server channel
the bot can see, although Kiro Crew immediately discards traffic outside
approved threads. Bot-authored messages (including our own) are dropped as a
loop guard. `DISCORD_BOT_TOKEN` is on the sandbox agent env denylist.

**Dispatch + rendering.** Turns ride the shared `TurnDriver`.
`transport_dispatch.py` carries the same mid-turn steer/queue/drain/cancel
machinery as the Telegram dispatcher (see "Mid-turn routing, queue receipts &
cancel" above) plus `!compact` under atomic `try_acquire` and the dashboard
mirror `!link`/`!unlink`. The renderer streams via throttled in-place edits
under the 2000-char cap (chunked at 1900 with fence-balanced splitting),
rotates messages at the shared driver's structured steer boundaries with quote
chips, renders trailing `[OPTIONS:]` as button action rows (`opt:<i>`, label
recovered from the component at interaction time), and posts Approve/Deny
buttons for interactive tool approvals. Approval `custom_id`s carry a
per-prompt random nonce (`a:<request_id>:<nonce>:<1|0>`) validated at
resolution: ACP request IDs are reusable across provider/gateway restarts, so a
stale button without the matching nonce fails closed. The decision window
denies by default on timeout and retires the nonce with it.

### Resume-binding expectations (`discord/resume_expectation.py`)

An inbound resume binding lives on the bound session's `session_map.json` row. A recycle, restart prune, or dashboard unlink can destroy that row and the only evidence the channel was attached, so the resolver silently falls back to its DM session; the expectation record makes that loss reportable.

**Store.** `$KIROCREW_HOME/trust/discord_resume_expectations.json` holds channel-id → `{key, title, version, retired}` rows under agent-blocked `trust/`, with an owner-only directory and `restrict_to_owner` file write because modes do not protect files on Windows. `retired` defaults false when loading an older row. Every filesystem step, including `config_dir()`, runs in a worker; an `asyncio.Lock` serializes read-modify-write without spanning Discord I/O.

**Refuse before route.** `DiscordSessionResume.route` returns one `RoutingDecision` containing either the session key or a refusal. Plain turns and session-targeting commands use that decision once; drained turns keep their enqueue-time native decision. `!new`/`!unlink` release every exact-channel binding, `!sessions`/`!help` remain reachable for recovery, and tool approval dispatches no turn while retaining its nonce-keyed visible failure path. Four states run: no owner/no record; no owner/retired record; one owner/no record (bootstrap); one matching owner/active record. Four refuse: active record without owner (lost link, retire after notice), any owner different from the active record or present beside a retired record (announce and adopt after delivery), multiple owners, or a resolution that keeps changing.

**Versioned acknowledgement.** Settlement follows a confirmed send and compare-and-sets the quoted version, so a newer picker/dashboard record wins and failed delivery settles nothing. A delivered detach replaces the active record with a durable retired marker in one write: no owner may route natively, while an owner racing the write still meets retained evidence and is refused before adoption. This avoids a clear-then-restore transaction whose compensating write could fail after evidence was deleted. **Persistence is fail-closed.** Memory publishes only after a durable write; only an absent file means empty, while I/O, UTF-8, JSON, shape, non-integer version, or non-boolean retired errors refuse routing. A pick records before binding. `!unlink`/`!new` serialize map removal, forced off-loop write, and versioned expectation retirement against pickers. Failed forced writes remain owed, keep the active expectation, and visibly fail the command; a later retirement failure costs one self-retiring notice rather than a silent resume.

**Gateway-wide by design.** One unreadable shared file may hide any channel's record, so all Discord routing refuses; a cached-channel exception would silently route the first unknown post-restart channel. Nothing overwrites, quarantines, or discards the file. *Repair:* stop the gateway, copy the file aside, restore or edit it to `channel_id → {key, title, version, retired}` with integer versions and boolean `retired` flags, then restart. Never truncate or delete it; `{}` is valid only when no channel has resume history. **One decision per message.** Route, refusal send, and settlement serialize per channel. Settlement waits for every message queued before the notice; each is refused, and only the last delivered notice settles. **Lifecycle.** This channel-keyed store detects loss but is not routing authority. If a channel-keyed binding authority lands, migrate these rows and delete this state machine.

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
  `allowed_user_ids` and `allowed_thread_ids` accept numeric snowflake strings
  only. Secrets land in `config_dir/.env` (atomic 0600) with `os.environ`
  synced; non-secrets go to
  `config.json` under `discord`. All fields are boot-read, so
  `restart_required` is true on any actual change.
## Telegram settings API

Two dashboard-only endpoints back the `/settings?tab=channels&channel=telegram` panel (legacy `?tab=telegram` links redirect there). Like the
Slack settings API they are registered in the dashboard route block (NOT
`_register_mcp_routes`) so they always sit behind dashboard token auth.

- `GET /api/telegram/config` — masked bot-token preview + presence boolean,
  `enabled` flag, `allowed_user_ids` (serialized as digit strings for the tag
  editor), `soft_threshold_pct`, forum per-topic config (`allow_forum` bool and
  `allowed_forum_chat_ids` — negative supergroup chat_ids serialized as strings
  for the tag editor), and live status: `connected` (true only
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
  `allow_forum` must be a strict boolean; `allowed_forum_chat_ids` accepts
  integer-like strings or ints and stores canonical deduplicated ints —
  supergroup chat_ids are NEGATIVE (e.g. `-1001234567890`), so the validator
  accepts a leading minus (NOT the digits-only check used for
  `allowed_user_ids`) and rejects non-integer garbage.
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

## WeCom settings API

- `GET /api/wecom/config` — the shared bot-channel shape with TWO credential
  slots: the panel's primary secret (`bot_token_set`/`bot_token_preview`)
  maps to `WECOM_SECRET`, and a second slot (`bot_id_set`/`bot_id_preview`)
  maps to `WECOM_BOT_ID`. `connected` is LIVE truth kept by the client's
  status callback: `maybe_start_wecom` wires `WeComClient.on_status` into
  dashboard state BEFORE opening the WS (so the first transition cannot be
  missed), and the reconnect loop reports transitions — healthy once a
  connection is up + subscribed; not-healthy with a reason on connect
  failure, an immediate server close (bad credentials), or a server kick.
  This callback is the compensating control for skipping save-time
  credential verification: bad credentials surface on the badge within
  seconds of the gateway starting, not silently never. `connect_error`
  carries that reason, `configured` requires both credentials AND
  enabled AND (a non-empty allow-list OR `allow_all_users`). `allowed_user_ids`
  projects the
  canonical `wecom.allowed_users` `{userid, name}` entries down to userid
  strings for the tag editor. `allow_all_users` is the explicit
  allow-everyone opt-in (default false) — it is a deliberate toggle, never
  inferred from an empty allow-list, and the transport still denies frames
  without a userid under it. Never returns a raw secret.
- `PUT /api/wecom/config` — requires a direct-local request (loopback peer
  AND no forwarding headers); remote gets 403. Validate-first/commit-last.
  Each credential slot has independent set/clear fields (`bot_token`/
  `bot_token_clear`, `bot_id`/`bot_id_clear`; clear flags must be strict
  booleans, an accidental `WECOM_*=` env-line paste is stripped, inner
  whitespace rejected). There is no pre-store verification: validating WeCom
  credentials needs the AI-bot WebSocket long-connection (no cheap REST
  "whoami"), so `verify_warning` is always empty; the live on_status
  badge (above) surfaces bad credentials within seconds of the channel
  starting. `allowed_user_ids`
  entries must match the WeCom userid shape (1-64 chars of
  letters/digits/`.-_@`, fail closed); the save re-attaches stored display
  names to surviving entries and writes the canonical `{userid, name}` list
  back to `config.json` under `wecom`. `allow_all_users` must be a strict
  boolean. Secrets land in `config_dir/.env`
  (atomic 0600) with `os.environ` synced. Writes are serialized under the
  repo-wide config lock. All fields are boot-read, so `restart_required` is
  true on any actual change.

## Feishu channel

**Transport (`kiro_crew/feishu/`).** A concrete `MessagingTransport` over
`lark-oapi` (`client.py`): inbound rides the lark-oapi WebSocket long
connection (a daemon thread pushing normalized `LarkInbound` frames into the
async event loop via `run_coroutine_threadsafe`); outbound is REST reply
anchored to the inbound `message_id` (via `run_in_executor` so it never
blocks the event loop). `lark-oapi` is an OPTIONAL dependency declared as the
`[feishu]` extra in `setup.cfg` and lazily imported inside the client module;
`maybe_start_feishu` catches `ImportError` and logs a skip so a missing
library never takes down the gateway. No public webhook endpoint is required.
Capabilities: `streaming=False`, `edit=False`, `reactions=False`,
`files_inbound=False`, `files_outbound=False`, `rich_blocks=False`,
`threads=False`, `max_message_chars=4000`, `max_buttons=0`,
`supports_proactive_send=False`. Long replies are split with
`split_markdown_safe` (the shared fence-safe splitter from
`messaging/split.py`), not `chunk_text`.

**Security model.** `authorize` is deny-by-default against
`feishu.allowed_open_ids` (frozen at construction); every denial is
SEL-audited (`source="feishu"`). Group-chat access is an explicit opt-in
gated on BOTH `allow_group=True` AND the group's `chat_id` appearing in
`allowed_group_ids`; every other context is denied with a SEL audit record
(`denied_group_not_allowed`). `FEISHU_APP_SECRET` is on the sandbox agent env
denylist.

**Dispatch + rendering.** Turns ride the shared `TurnDriver`
(`transport_dispatch.py` mirrors the WeCom dispatcher: `/new`, `/compact`
command intercept, mid-turn messages fold into the running turn via steer
gated on `has_active_turn`, `/compact` under atomic `try_acquire`, soft/hard
context-threshold notices as separate proactive messages). The dispatcher
runs decider-less (no interactive buttons); an `[OPTIONS:]` trailer is
stripped. The renderer buffers the complete turn and sends it as one reply on
`on_done` — no streaming, no edit-in-place.

**Session identity.** A p2p turn keys on `open_id` under the `DIRECT` chat
type; a group turn keys on the group's `chat_id` under the non-direct
(`FORUM`) chat type, so a group turn never resumes the sender's private DM
bucket — including under `dm_scope: unified`, where a direct key collapses to
the cross-channel bucket and a group key deliberately does not.
