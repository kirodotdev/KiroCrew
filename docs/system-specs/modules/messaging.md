# Messaging Transport Module

## Overview

`kiro_crew.messaging` is the channel-neutral transport abstraction used by the shipped Slack, Discord, Telegram, Webex, WeCom, Microsoft Teams, Weixin, iMessage, WhatsApp, and Feishu integrations; its conservative contract also leaves room for a further channel. It avoids re-implementing streaming, tool approval, session identity, or rendering for each integration. It holds the channel-neutral core of the Slack turn loop (`slack/handler.py::handle_message`) so a new channel implements only two small interfaces (a `MessagingTransport` + a `Renderer`) and inherits everything else.

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

> **No channel-local approval grant, and no command-redirect seam.** Slack carries
> both — a named `is_yolo_mode` / `set_yolo_mode` wrapper, and a `_BANG_TO_SLASH`
> map from `!cmd` to `/kirocrew cmd` — and neither is replicated on another channel.
> The grant underneath the wrapper is the shared `safety_override`, so a second name
> only creates the opportunity for a second source of truth; a grant is global by
> nature and an operator turning auto-approve off expects it off everywhere. The
> redirect map is a promise to keep two grammars alive, and three of its entries
> already point at sub-commands that are not registered. A channel may have ALIASES
> (several spellings resolving to one canonical command name), which is a different
> thing: an alias does not tell the user to type something else. Pinned by
> `test/test_telegram_parity.py::TestNoChannelLocalGrantOrRedirectSeam`.

| File | Purpose |
|------|---------|
| `messaging/__init__.py` | Package facade re-exporting the public contracts, approval-mode constants, and Layer-3 helpers |
| `messaging/transport.py` | **Layer 1** — `MessagingTransport` ABC + the `TransportCapabilities`, `InboundMessage`, and `ConfiguredChannelTarget` value objects (stdlib-only) |
| `messaging/driver.py` | **Layer 2** — `TurnDriver` (channel-neutral turn loop), approval-mode constants, `_redact` helper |
| `messaging/renderer.py` | **Layer 2b** — `Renderer` ABC, `OutputEvent`, output-kind constants + `OUTPUT_KINDS`, `chunk_text` helper, `session_provenance_tag` (stable callback affinity without exposing session keys), `apply_options_cap`/`cap_choices`/`format_overflow` (`max_buttons` enforcement), `split_options_trailer` (the ONE trailer parse for the marker family — see below), and `render_options_as_text` — the whole-trailer path for a channel with no widget, which reaches the same cap with zero slots so every choice becomes a numbered line. Also `credential_redaction_notice(count)` — the one sentence a channel sends when credential redaction rewrote text it already delivered, so the reader learns a pasted command will not run. Shared so the wording cannot fork per channel and each spelling need its own audit for leaked bytes; it carries only the count, never secret bytes, and is plain text with no markup or emoji because one string ships to platforms that render different dialects (or none) |
| `messaging/approval.py` | Two channel-neutral approval styles behind one INTERACTIVE `decider`, both deny-by-default on timeout and keyed `session_key`+`request_id`. **Typed reply** (`TEXT_APPROVAL_TIMEOUT_S`, the verdict vocabulary, `TextReplyApprovalDecider`) for a `max_buttons=0` channel, with Trust recorded as the session's own approval policy rather than a second trust store. **Widget awaiter** (`PendingApprovals` + `SessionApprovalDecider`) for a press whose correlation id and per-prompt nonce travel a round trip this module cannot see (a Webex Adaptive Card over the device websocket); a typed answer has no nonce, a press has no free text |
| `messaging/driver.py` `deny_all_tools` | Rejects EVERY permission request ahead of every approve path. The approval ladder cannot express "this sender is not the operator" on its own: the PreToolUse hook may answer `auto_approve` and the Trust/YOLO predicates approve and short-circuit, both BEFORE the ladder is consulted, so setting the mode to `interactive` without a decider is not sufficient. Defaults False |
| `messaging/display_safety.py` | `strip_ansi` / `canonicalize_display` / `redact_for_display` — credential redaction against the form a platform RENDERS, not the bytes sent. Hoisted out of `slack/format.py` when the shared overflow sink began writing choice text into the parsed body on every widget channel |
| `messaging/markup.py` | `strip_thinking_tags` / `flatten_pipe_tables` / `flatten_mermaid_body`: Markdown reductions for a surface that renders none of the source form (a `<thinking>` block, a pipe table needing a monospace grid, a `mermaid` fence needing an image). Emits Markdown, never a channel dialect, so each channel's own inline converter finishes the job. Stdlib-only leaf |
| `messaging/split.py` | `split_markdown_safe` — the shared fence-safe markdown splitter (stdlib-only, pure). Prefix-stable so streaming callers can send sealed chunks and keep only the last as a live buffer. `split_markdown_bytes` wraps it for a byte-capped platform, measuring the produced chunks and shrinking the character budget until they fit, with the `chunk_utf8_bytes` primitive as the floor. Also exports `iter_fence_spans`, the same fence machine viewed as character spans over a whole message |
| `messaging/outbound_files.py` | `extract_local_refs` (+ `extract_local_refs_off_loop`) — pulls local markdown image references out of an outbound reply into `OutboundFile` payloads carrying the validated bytes, with `Rejection` reasons for everything refused. Also `iter_local_refs` / `hide_local_refs`, the text-only scan a streaming channel uses to keep the markup off live frames. Channel-neutral; the upload stays per-transport |
| `messaging/raster.py` | `sniff_raster_mime` — what counts as a raster, decided by leading bytes. Dependency-free (no `kiro_crew` imports) so both the inbound sniff and the outbound extractor can share it |
| `messaging/tables.py` | `render_tables` + the `off`/`cards`/`grid`/`native`/`auto` policy contract and `display_width` — outbound Markdown-table rendering for a target that shows a pipe table as literal pipes (stdlib-only, pure) |
| `messaging/status_reactions.py` | `PhaseReactionLadder` plus the turn-status line (`format_turn_status`): debounced phase-to-emoji swapping over an injected `ReactionSink`, a stall watchdog, tool-to-phase classification, and `merge_phase_emojis` for a user's overrides. Owns no channel API and no emoji vocabulary |
| `messaging/commands.py` | The channel-neutral half of the shared chat commands — `/stop`'s cancel + lock ordering (`stop_running_turn`), `/yolo`'s grant ladder (`run_yolo_command`), and the dashboard-link TTL vocabulary (`parse_dashboard_ttl` / `format_ttl`). Returns reply TEXT, never sends; takes no address of any kind. Also the path-independent keyword commands as one copy of their reply text — `spawn`/`bg`, `cron list|remove|pause|resume`, `task run|status|cancel`, each `(text, service) -> reply | None` where `None` means "not this command, keep routing". NOT the runtime stats line: both channels call `Stats().summary()` directly, because a one-liner with no parsing and no service to duck-type gains nothing from a shared copy. `spawn_task_reply` / `cron_remove_all_reply` are the already-parsed forms a channel whose own grammar carries the prefix calls. The services are duck-typed under `TYPE_CHECKING` because `subagent` and `taskrunner` both reach `slack` transitively |
| `messaging/sessions_view.py` | The channel-neutral recent-sessions collector (`_collect_recent_sessions` + its off-loop form). Takes an explicit `sessions_dir` so a surface owning its own data-home override threads it in rather than shadowing this module's; `slack/sessions_view.py` does exactly that and keeps the Block Kit rendering |
| `messaging/privacy_mode.py` | The `!temporary` / `!incognito` session privacy modes, keyed by **session key** — two bounded LRUs, the durable `SessionMap` flag, one `is_restricted` predicate, the token strippers, and the SEL audit with the channel as a parameter. See [Session privacy modes](#session-privacy-modes-privacy_modepy) |
| `messaging/auto_title.py` | Conversation auto-titling — the claim-early LRU, the tool-free bounded background turn, the prompt, and the title-cleaning rules. Renaming the platform conversation is a caller-supplied callback. See [Auto-titling](#auto-titling-auto_titlepy) |
| `messaging/upload_gate.py` | `uploads_restricted(dashboard_state, session_key, channel_type=)` — the restricted-session ceiling on outbound file uploads, plus `live_dashboard_slot`. Three-state ladder (non-`dashboard:` key allows, a LIVE slot answers off `is_restricted`, otherwise the PERSISTED transcript mode answers and an unreadable one DENIES), audited per channel. Discord and Telegram both route here |
| `messaging/session_trust.py` | The per-session tool-Trust grant store: `is_session_trusted`, `add_trusted_session(key, sessions=)`, `clear_trusted_sessions`. In memory only, so an ad-hoc auto-approve grant dies with the process. The grant has TWO halves and both are load-bearing: the in-memory mapping the driver reads, and the session's approval policy set to `auto`, because a spawned subagent reads its parent's policy and never this mapping. So it is a `key -> SessionManager` MAPPING rather than a set, which is what lets `clear_trusted_sessions` undo the policy half too (back to `""`, the same value the dashboard's untrust toggle writes) without its caller having to hand a manager back. **Every mutation goes through the API**: reaching the container directly is how a revoke came to drop one half and leave subagents trusted, and a mapping has no `.add`, so a half-grant is not expressible either. Named `session_trust`, not `trust`, so it cannot be confused with a connection-admission roster: this grant is about what ONE session's tools may skip, not about which principals may attach. Consumed only through `TurnDriver`'s `auto_approve_session` predicate, which runs BEHIND the keystone, governance and deny-list gates, so a hard DENY still refuses |
| `messaging/link.py` | **Layer 3** — session-key namespacing (`session_key`/`canonical_key`/`legacy_key`/`is_legacy_slack_key`) + `ChannelLink` + DM-scope key derivation / `should_rotate_generation`, plus the in-channel `/link` ⇄ `/unlink` pair (`rebind_conversation_location` / `release_conversation_location`) |
| `messaging/conversation.py` | `ConversationState` — per-conversation rotating *generation* bookkeeping (advanced by `/new` and idle/daily reset), seeded from the persisted session map |
| `messaging/session_resume.py` | **Layer 3** — the channel-neutral half of dashboard-session resume. `SessionResumeController` consumes a bound `ResumeSurface` (including its exact durable expectation identity, which may be narrower than `ChannelLink.channel_id`) and owns the complete SHOW-PICKER flow (eligibility/search, audit, nonce/TTL/owner/message scoping, and registration only after a successful post) plus the CHOOSE/BIND transaction (history existence, conflict checks around the awaited settlement, durable expectation before success, atomic inbound claim, lost-claim/storage outcomes, dashboard push, and audit). `SessionBinder` owns inbound routing, settlement, and release. Discord and Teams retain only address/identity derivation, widgets/cards, exact wording and display redaction, callback parsing, and channel-local replay. |
| `messaging/resume_expectation.py` | The durable conversation-keyed shadow of those bindings, ONE file per channel (`store_filename`), because a Discord channel id and a Teams conversation id are unrelated address spaces |
| `teams/service_urls.py` | `ServiceUrlStore` — durable `conversation_id -> serviceUrl` (plus the authorized identity owning each conversation), because the Bot Framework offers no lookup and a lost reference leaves every proactive path with nowhere to send. `forget` drops a route the Connector permanently refuses |
| `teams/cards.py` | Adaptive Card construction + `parse_submit` — the strict, total validation of an untrusted card payload. Mints no nonce of its own: every clickable widget's token comes from `messaging.renderer.new_approval_nonce` |
| `teams/approvals.py` | `TeamsApprovalDecider` — awaits one Approve / Approve+auto-approve / Deny click, deny-by-default on every non-answer. Holds NO grant: the button's press is recorded and the dispatcher arms the shared process-wide grant through `messaging.commands.run_yolo_command` |
| `teams/session_resume.py` | Teams' half: the Adaptive Card picker, its display redaction, and the owner rule — which is STRICTER than Discord's for a reason (see the Teams section) |
| `teams/attachments.py` | Teams' file halves — the two inbound shapes with OPPOSITE fetch auth, the inline-image outbound policy, and `quoted_reply_text` (a 1:1 quote-reply's own words, which `activity.text` does not carry cleanly) |
| `slack/transport.py` | Slack reference `MessagingTransport` (`SlackTransport`) over `SlackClientOps` |
| `slack/renderer.py` | Slack reference `Renderer` (`SlackRenderer`) + `SlackApprovalDecider` + `build_approval_blocks` |
| `slack/transport_dispatch.py` | `handle_message_transport()` — full new-path dispatch wiring the three layers together |

## Layer 1 — `MessagingTransport` (`transport.py`)

Channel-neutral inbound/outbound contract. A new channel = implement this interface + an inbound adapter, with zero change to the shared turn-handling core.

- **Class attributes**: `channel_type: str` (e.g. `"slack"`) and a `capabilities: TransportCapabilities`.
- **Tier-1 core (abstract)**: `send_message(conversation_id, content, thread_id=None) -> str` (returns a platform message id), `resolve_conversation(user_id) -> str` (the `open_dm` equivalent), `fetch_history(conversation_id, thread_id=None) -> list[InboundMessage]`.
- **Lifecycle (default no-op, override as needed)**: `connect()` (lazy-import client libs HERE), `maintain()` (poll/heartbeat), `disconnect()`.
- **Inbound adapter (abstract)**: `receive(raw_envelope)` (ack → filter → authorize → normalize → dispatch) and `authorize(msg) -> bool`. `authorize` MUST be **deny-by-default** — an unconfigured transport authorizes nobody.
- **Outbound authorization**: `may_send_to(conversation_id, thread_id=None, *, principal="") -> bool` re-decides recipient authorization for a **proactive** send. `authorize` gates a turn the user drove; this gates the messages nobody asked for (a cron result, a compaction notice, a subagent completion), which resolve their destination from a *persisted* `ChannelLink`. A link records a conversation but **not the principal that authorized it**, so without this a recipient removed from a channel's allow-list kept receiving proactive traffic after a restart: the roster changed and nothing re-read it. Only the transport can answer, because the roster holds principals while the link holds a conversation id and whether those are the same string is a per-platform fact. `principal` carries the peer's platform id when the session key names one, which is what lets a transport with an opaque conversation id (Discord, Webex) reach its roster at all; empty means the key names no single person (a room route, a unified bucket), NOT that nobody is authorized. MUST stay **synchronous and in-memory**, because it runs on every proactive send: a network round trip there is unbounded work on the send path, and a check that can time out is a check that fails open under load. See § Proactive sends for where it is enforced and which channels answer how.

### `TransportCapabilities`

Declares what a channel can do. Defaults are deliberately conservative (the WhatsApp-like floor) so a transport that forgets to declare a capability degrades safely rather than over-promising.

| Field | Default | Notes |
|-------|---------|-------|
| `streaming` | `False` | feature flag |
| `edit` | `False` | feature flag |
| `reactions` | `False` | feature flag |
| `files_inbound` | `False` | feature flag — the two directions land per channel and in different changes, so ONE `files` boolean was undecidable and got the wrong answer for one of them |
| `files_outbound` | `False` | ENFORCED — gates whether a renderer extracts and uploads a local image reference. Declaring `False` keeps printing the path, which is the honest degradation |
| `rich_blocks` | `False` | ENFORCED — gates whether a renderer attaches a native widget at all (Webex reads it for both the approval Adaptive Card and an `[OPTIONS:]` card). Declaring `False` keeps the numbered-text and typed-reply forms, so it decides whether a widget APPEARS, never whether the user can answer |
| `threads` | `False` | feature flag |
| `table_mode` | `off` | outbound table presentation: `off` / `cards` / `grid` / `native` / `auto`; read only by renderers that use `render_tables_for_target` |
| `native_tables` | `False` | the target renders a GFM pipe table AS a table; checked before `native` may pass through |
| `supports_session_resume` | `False` | ENFORCED — gates whether a dashboard connect marks the binding as an inbound resume target (`direction: both`). Only a transport whose inbound path resolves the mirror binding may declare it |
| `max_message_chars` | `4096` | quantitative — Slack 3900, Telegram 4096, Discord 2000, Teams 16000, WhatsApp 4096. A CHARACTER count: a byte-capped platform must declare a value safe at its worst-case bytes-per-char (Webex and Teams are pinned in `test_capability_ledger.py`) |
| `max_message_bytes` | `0` | quantitative — the platform's REAL budget when its cap is denominated in UTF-8 BYTES, which `chunk_for_transport` measures against instead of the character floor. `0` means "no byte cap" and is the honest default: a char-capped transport that declared one would chunk against a unit it does not have, and a byte-capped transport that omits it merely keeps the 4x-pessimistic `bytes // 4` char floor. Only Webex declares it today; WeCom is byte-capped and deliberately stays on the char floor. Pinned both ways in `test_capability_ledger.py` |
| `max_buttons` | `3` | TOTAL interactive choices per prompt (the WhatsApp Business Cloud API's reply-button cap, which is where the default came from; the personal-account WhatsApp channel this repo ships declares 0); enforced via `apply_options_cap` -- overflow degrades to a numbered text list |
| `mention_grammars` | `True` | ENFORCED — whether the platform parses a broadcast-mention grammar (`@everyone`, Slack's `<!channel>`) in a message body. `messaging.renderer.display_safe_for` reads it at the channel-NEUTRAL proactive sinks and applies the zero-width-space defang only where one exists. Default `True` because the directions are asymmetric: a needless defang mangles text cosmetically, a missing one lets a prompt-injected `@everyone` mass-notify. Webex declares `False` — no broadcast grammar, and its allow-list IS email addresses, so the defang makes every address the agent prints uncopyable |
| `supports_proactive_send` | `True` | send-policy (the WhatsApp Business Cloud API is `False` outside its 24h window; the personal-account channel here has no such window and declares `True`) |
| `returns_message_id` | `True` | ENFORCED — whether `send_message` answers with a real id, so a caller may read an EMPTY id as "refused". WeCom's proactive command and Feishu's reply carry none, so for them `""` is the SUCCESS value and failure RAISES; both declare `False`. Asked through `delivery_confirmed(capabilities, message_id)`, never re-derived per call site |

`to_dict()` serializes all fields. The integer *parameters* (not booleans) capture where channels differ quantitatively so the `Renderer` can chunk / degrade rather than assume a single shape.

**A proactive send is CHUNKED against the transport's own cap, then confirmed part
by part.** A transport caps by SLICING (Telegram's `_cap_text` at 4096) and still
answers with a message id, so handing it a longer message loses the tail and reports
success — which the caller then audits as a completed delivery. Both legs in
`dashboard/handlers/messaging.py` split first and stop on the first unconfirmed part,
since the remaining chunks of a message whose head never landed would arrive as an
orphaned fragment. The two legs keep separate loops deliberately: one splits plain
text and the gateway's splits markdown with fence sealing, so collapsing them would
silently retune one of the two.

**A send that did not raise is not automatically a delivery.** `delivery_confirmed`
(`messaging/transport.py`) is the one predicate the three proactive-send call sites
ask — `slack/gateway.py`'s channel reply leg and both legs in
`dashboard/handlers/messaging.py`. Most transports report a refused or exhausted
send by returning an empty id rather than raising, and that return value is
load-bearing: cron stands its Slack fallback down and advances its dedup hash on a
confirmed delivery, so one false success loses the result on every surface at once.
The two id-less transports invert it — applying the empty-id test to them reports
every delivered cron result as lost, leaves the dedup hash unadvanced, and repeats
the same result on the next tick. Which is why the reason lives in the predicate:
it had been written out at each call site, and the copy that forgot the second
convention was the one that shipped. `test_channel_transport_outbound_authz.py`
pins the declaration against each transport's own `send_message` by AST, in both
directions, so a transport cannot declare a convention it does not follow.

### `InboundMessage`

Normalized, channel-agnostic inbound message: `channel_type`, `user_id`, `conversation_id`, `text`, `thread_id=None`, `attachments=[]`, `is_mention=False`; `to_dict()` for serialization.

## Layer 2 — `TurnDriver` (`driver.py`)

Consumes a provider's `AcpEvent` stream and emits abstract `OutputEvent`s to a per-transport `Renderer`. It owns the channel-neutral turn concerns — credential/exfiltration redaction and the tool-approval decision — so every channel inherits them once.

**Redaction and protocol framing** — before text reaches a renderer, `TurnDriver` first classifies a reserved summary-bearing compaction notice at the start of the turn, then incrementally parses kiro-cli's inline `[STEERING steer-<id>: …]` frame across arbitrary chunk boundaries. Compaction summary bodies become the terse `✅ Context compacted.` receipt. Steering frames never become text: they emit one structured `STEER_CONSUMED` event at the exact boundary (paired with kiro-cli's typed lifecycle event regardless of arrival order). The user-facing `[OPTIONS: …]` trailer is deliberately not part of this filter and passes through unchanged for renderer-native buttons. After framing, `_redact()` runs `redact_exfiltration_urls()` then `redact_credentials()` (both from `security.py`) over every text chunk, thinking chunk, tool title/purpose, and each string field of prompt-choice options before it reaches a renderer.

The dashboard does **not** flow through `TurnDriver`; it remains unchanged as the authoritative transcript surface. Direct channel paths that bypass the driver are sanitized at source: Discord's explicit five-message resume replay strips legacy steering frames and summary-bearing compaction notices, shortens each entry to the shared splitter's first (sealed) chunk so a replayed code block cannot arrive with its fence cut in half, and puts the role icon on its own line so the body's first line still starts where the fence grammar needs it; direct compact commands publish only terse receipts. Stored transcripts remain intact for audit.

**Session-directive consumption** — an optional `directive_consumer` callback (`(kind, args) -> awaitable`) makes the driver the channel-side consumer of the stateless session-directive protocol (`session_directive.py`): the trusted `_meta.kiro` identity is resolved by the shared forgery-gate predicate (`session_directive.directive_tool_for(mcp_server_name, tool_name)`, the same single spelling the dashboard consumer uses) and recorded at `EVENT_TOOL_CALL`, and the matching `EVENT_TOOL_RESULT`'s marker is decoded and handed to the consumer — single-consume across result frames, forged markers under any other tool ignored, `encode()` refusals logged, a lost marker on the final frame logged at WARNING. A tool call announced as a NATIVE sub-agent's (`EVENT_SUBAGENT_ACTIVITY` with a `tool_call_id`) is refused with a SEL `denied` audit rather than applied — a child session must never arm/mutate its parent, mirroring the dashboard consumer's isolation. Dispatchers inject `messaging.dispatch.build_directive_consumer(session_key=…, sessions=…, dispatcher=…)`, which funnels into the same `apply_session_directive` core the dashboard consumer uses with `slot=None` (so card-producing dashboard-only directives stay refused for channel turns). Channel `set_project` writes the durable per-conversation project/CWD override; because its tool result arrives while the current provider still owns the turn semaphore, the provider is not killed in place. The next claimant acquires the old semaphore, replaces that provider, and cold-starts in the new CWD before sending its prompt. Legacy prompt loops take effect where the session is nudge-able (`slack:`/`discord:`/`webex:`). Structured monitor creation is narrower: dashboard, Slack, and Discord have typed dispatch plus completion correlation, while Webex is refused before a structured record is armed. On the other five transports (Telegram, iMessage, Teams, WeCom, Weixin) the applier likewise answers "not supported from this session type" — logged and SEL-audited instead of the old silent drop. Being on either list takes both an admitted binding and the matching fire contract; otherwise a loop can report itself healthy before dying on its first cycle. Without a consumer, directive markers are inert exactly as before.

**`run(message) -> str`** — calls `renderer.on_turn_start()`, then translates each provider event into a dispatched `OutputEvent` and returns the accumulated (redacted) assistant text:

| Provider event | Emitted `OutputEvent` |
|----------------|-----------------------|
| `EVENT_TEXT_CHUNK` | `TEXT_CHUNK` (protocol-framed, redacted, accumulated); inline steering frames become `STEER_CONSUMED`, compaction summary notices become a terse receipt |
| `EVENT_THINKING_CHUNK` | `THINKING` |
| `EVENT_STEER_CONSUMED` | paired with the inline frame so exactly one `STEER_CONSUMED` boundary reaches the renderer |
| `EVENT_TOOL_CALL` | `TOOL_CALL` (uniform — each call completes the prior task + starts a new one); records the directive-tool identity when a `directive_consumer` is injected |
| `EVENT_TOOL_RESULT` | nothing rendered; decodes + applies a session-directive marker via the injected `directive_consumer` (inert without one) |
| `EVENT_PERMISSION_REQUEST` | `PROMPT_CHOICE` (interactive w/ decider only) then approve/reject |
| `EVENT_COMPACTION_STATUS` | `COMPACTION` |
| `EVENT_COMPLETE` | `DONE` |

An optional structured-monitor completion hook is orthogonal to rendering. It
revalidates the persisted `(monitor_id, fingerprint)` claim after
`renderer.on_turn_start()` and immediately before the provider stream begins.
Channel monitor adapters also inject the synchronous SessionManager
`begin_turn` gate after that authorization and before marking the hook accepted;
the gate and stream entry have no intervening await, so a gateway shutdown that
began after the session lease was claimed refuses the wake instead of opening a
turn outside the shutdown drain snapshot. The checks and stream entry have no
intervening await; refusal returns without
entering the provider, recording a successful turn, or persisting/mirroring the
revoked wake, and does not mark the hook accepted. If channel context building,
conversation lookup, or dashboard rehydration observes that the structured claim
has already been stopped and therefore cannot construct its completion hook, the
adapter returns `UNAVAILABLE` before dispatch rather than falling through to the
ordinary/legacy turn path. Dashboard performs the
equivalent final check after any unattended background-turn permit and before
appending the wake or entering its runner. The hook is invoked exactly from the
`EVENT_COMPLETE` branch when its reason is safe completion evidence, before
buffered output, steering, redaction, or `DONE` is flushed to the renderer, with
the event's `TurnUsage` and a disposition derived from its stop reason. ACP's
stale-stream compatibility completion reuses `end_turn`, so that reason remains
uncharged until the event carries provenance that distinguishes it from a provider
result. Completion accounting therefore survives failure or cancellation during
renderer finalization. A normal handler return, command intercept, stream exception,
or ACP-synthesized terminal does not manufacture completion evidence. Callback
failure is logged and cannot
change the channel turn's output or error behavior. The hook is absent from ordinary inbound turns
and legacy AutoNudge turns. Slack reports a raw completion before its cancellable,
best-effort analytics usage-row write. If shutdown cancellation lands after the
raw event but before the driver returns, the adapter reports the captured
completion once before propagating cancellation, so an accepted wake cannot remain
in flight. A shutdown refusal before stream entry is transient `BUSY`, not terminal
`UNAVAILABLE`, and retries the durable claim after restart. Directive consumption
occurs before a later raw `EVENT_COMPLETE`, so an action that calls `monitor_stop`
or the structured `autonudge_stop` alias first makes its monitor inactive with a
durable `user_stop` outcome while retaining only the current wake correlation. The
turn-start hook records that acceptance synchronously in runtime state because the
outer dispatcher cannot persist `DISPATCHED` until the channel turn returns; this
marker distinguishes an accepted turn from a `BUSY` claim with the same unset
durable delivery field.
The completion hook then charges and clears that claim exactly once. Without safe
completion evidence the terminal record remains uncharged and cannot probe, re-arm,
or redispatch.

The structured monitor controller probes before entering any channel turn.
No-change, record-only, provider-retry, and terminal decisions therefore call
no messaging dispatcher and consume no model turn. For a newly actionable
fingerprint it persists the in-flight claim first, then supplies one redacted,
4,096-character-bounded `[Monitor wake]` envelope to the dashboard, Slack, or
Discord adapter without legacy cycle decoration. Each adapter returns the same
typed handoff: `DISPATCHED` starts the durable bounded completion-evidence
deadline, `BUSY` retries the already-claimed wake without another probe or model
turn, and `UNAVAILABLE` becomes a retained terminal outcome. A stream that ends
without raw `EVENT_COMPLETE` remains dispatched until its evidence deadline; it
never fabricates completion or immediately retries the same fingerprint.
Discord decides the structured result at its own dispatch boundary, not from the
scheduler's earlier advisory busy check: a concurrently busy session returns
`BUSY` without steering or queueing the wake, a refusal before the turn returns
`UNAVAILABLE`, and `DISPATCHED` is returned only after the claimed session has a
started `TurnDriver` carrying the matching completion hook. After its advisory
acceptance marker is set, a later Discord cleanup or dispatch exception remains
`DISPATCHED`; the durable completion-evidence deadline, rather than a contradictory
terminal-unavailable handoff, owns recovery for that accepted turn. After its advisory
check, Discord takes the SessionManager's non-waiting semaphore lease before
renderer setup, upload-policy lookup, typing, attachment work, or any other
pre-turn await. If a user turn wins that authoritative claim boundary, the
monitor returns `BUSY` immediately and creates no steer, queue entry, or
completion evidence. A structured wake also skips idle and daily generation
rotation after the gateway validates its session key, so the dispatcher claims
the exact generation that authorization approved. The gateway passes that key
through to the dispatcher, which rechecks it immediately before the non-waiting
session claim and refuses a generation changed by a concurrent `!new`. The
scheduler propagates that typed result unchanged. Discord repeats the same positive
generation check in the driver's closing gate after claim authorization and all
pre-stream awaits, so a later `!new` cannot send the wake into the abandoned session.
Slack rechecks the runtime inbound-channel policy before every structured wake,
because the policy can become stricter after monitor creation. After its advisory
busy check, Slack also takes the SessionManager's non-waiting semaphore claim; a
user turn that wins that boundary returns `BUSY` without waiting, starting a turn,
or creating completion evidence. A claimed Slack structured wake runs through
the same `TurnDriver` directive consumer as ordinary channel turns, so an
authenticated `monitor_update`, `monitor_stop`, or structured
`autonudge_stop` result is applied to that Slack session before a later raw
completion is accounted; legacy nudges retain their existing collector path.
If Slack's bounded timeout fires before the structured completion hook is
accepted, the claim is retryable and returns `BUSY`; after acceptance it remains
`DISPATCHED`, and the durable completion-evidence deadline owns recovery.
Discord applies the same bounded outer turn timeout to legacy nudges and
structured wakes, so a wedged dispatcher cannot hold the scheduler forever. A
timeout before the structured completion hook is accepted returns
`UNAVAILABLE`; a timeout after acceptance remains `DISPATCHED`, and the durable
completion-evidence deadline owns recovery for the correlated turn.

### Approval ladder

Four modes (constants, mirroring the native Slack + dashboard ladder):

| Constant | Value | Behavior in `_approve()` |
|----------|-------|--------------------------|
| `APPROVAL_AUTO` | `"auto"` | approve |
| `APPROVAL_TRUST` | `"trust"` | approve |
| `APPROVAL_TRUST_READS` | `"trust-reads"` | approve iff `event.tool_kind == "read"` |
| `APPROVAL_INTERACTIVE` | `"interactive"` | **deny-by-default** unless the injected `decider` approves |

Two injected predicates take precedence over the ladder (both checked per permission request, and both auto-approve immediately — no buttons, no decider wait):

- `auto_approve_tool: (permission_event) -> bool` — hook-driven auto-approve (e.g. `spawn_run` via the context builder's `auto_approve_subagent_spawn` hook). The predicate receives the whole PERMISSION EVENT — never just the title, which is model-authored — and keys on canonical identity only (`hooks.event_is_spawn_run`: `tool_name` == `spawn_run` with the `mcp_identity_trusted` provenance flag, served by `CORE_MCP_SERVER`; no title fallback — an event without canonical identity falls to the ladder below). Reason logged as `hook_auto_approve`.
- `auto_approve_session: () -> bool` — honors the auto-approve grant without the driver importing any channel module. Reason logged as `session_trust`. **Every shipped channel passes it**, and for a decider-less one it is the ONLY rung. Webex, WeCom, iLink (weixin), iMessage, Discord and Teams pass the same `() -> safety_override().is_active()`, the ONE process-global grant the dashboard toggle drives. Telegram passes `safety_override().is_active() or is_session_trusted(session_key)`, because it offers a Trust button and so needs the per-session grant as well; the grant it reads is the SHARED `messaging/session_trust` one, which is the distinction that matters here. Slack is the outlier, passing a narrower channel-local `is_slack_session_trusted`. **A new channel should follow the seven, not Slack.** A channel-local trusted set is a SECOND grant: its own lifetime, its own audit trail, and its own way to disagree with the dashboard about whether auto-approve is on — and "is YOLO on?" has to have one answer. Omitting the keyword entirely is not a neutral default either: it makes arming YOLO from the dashboard INERT on that channel, so an unattended run still stops on every tool prompt with nobody there to answer, which is how Discord shipped until it was enrolled.

**A `tool_gate` `"auto_approve"` verdict for a shell command is name-grant
verified before it is honoured.** The gate's hook grants by program NAME
(`auto_approve_tools` globs, the read-only allowlist), and the shell resolves
that name again through a `PATH` that can lead with agent-writable directories.
The driver therefore awaits `name_grant.refusal_for_event(event)` at the honour
point — the one place shared by every channel's gate, chosen because each
channel's `_tool_gate` is synchronous and loop-bound while the check does
filesystem work — and on a refusal DOWNGRADES to the ladder below (never a hard
block), logging `outcome=auto_approve_declined` with `reason=name_grant`, the
refusal code, and `tier=hook_auto_approve`. On Windows the check cannot model
the shell's lookup at all, so it declines every name-based shell grant there —
a channel turn without a decider then falls to deny-by-default for shell tools
its `auto_approve_tools` used to grant. Non-shell verdicts and the two
full-trust predicates above are not name-based grants and are unchanged. The
`APPROVAL_TRUST_READS` rung is also unchanged and deliberately out of this
check's scope: it keys on `event.tool_kind`, never on a program name — a
kind-based grant with its own (weaker) trust model, unlike the dashboard's
trust-reads tier, which classifies the command by program name via
`is_read_only_bash` and is therefore name-grant verified there.

**Teams has a decider AND the shared predicate, and that combination is the
pattern.** It renders Adaptive Card approvals, so it passes a real `decider`; it
also passes the same `() -> safety_override().is_active()` the buttonless channels
do, because it keeps NO grant of its own. Its `/yolo` goes through the shared
`run_yolo_command`, and its card's middle button arms that identical grant through
the identical helper — so the command and the button cannot diverge, and neither can
disagree with the dashboard. The button is therefore labelled "Approve +
auto-approve" rather than "Trust session": the blast radius is every surface until
the grant expires, and a control has to say what it does. The label alone is not
enough, so the card body also carries `messaging.commands.YOLO_SCOPE_NOTE` — the one
sentence naming what the grant covers, shared with the reply `/yolo on` returns so a
pre-press affordance and the confirmation cannot describe different scopes.
"Auto-approve" on a button inside one chat otherwise reads as scoped to that chat.
Expiry, renewal, the duration and the SEL row all belong to the shared helper, which
is the point — a channel-local store would have had to reimplement every one of them.

`decider: ApprovalDecider` (`Callable[[Any], Awaitable[bool]]`) supplies the interactive click; when omitted, interactive mode denies by default (so buttons are only rendered when a decider exists — otherwise the user would get dead controls). Every permission decision emits an `sel().log_api_access` event (`caller="turn_driver"`, `operation="tool_permission"`, `source="messaging"`, `outcome` one of `auto_approved` / `approved` / `denied`).

**Deny-on-silence can be SPOKEN.** `open_approval(..., on_timeout=…)` takes an optional coroutine that `PendingApproval.wait` awaits when the window closes, before it returns `DENY`, so the channel can resolve the prompt still sitting on the user's screen: `approval.TIMEOUT_NOTICE` is the text. Without it the refusal is invisible: the turn moves on and a live-looking prompt remains, which a later `1` can no longer answer (it finds no open entry and gets `RECEIPT_EXPIRED`). It is a callback rather than a transport because this module never learns what a channel is, and only the renderer that posted the prompt knows which message to edit. It must not raise: the verdict is already `DENY`, and `_announce_timeout` logs and swallows anything but cancellation, because a notice that could not be posted must never become an approval. WhatsApp is the first channel wired onto it.

## Layer 2b — `Renderer` + `OutputEvent` (`renderer.py`)

### `OutputEvent`

Channel-neutral output event with a `kind` plus per-kind payload fields (`text`, `tool_call_id`, `title`, `tool_kind`, `tool_purpose`, `options`, `request_id`, `context_usage_pct`, `stop_reason`); `to_dict()` serializes them. Kinds: `TEXT_CHUNK`, `THINKING`, `TOOL_CALL`, `PROMPT_CHOICE`, `COMPACTION`, `DONE` — the full set is `OUTPUT_KINDS` (a `frozenset`). `prompt_choice` is a **first-class** event, not generic "permission text": each renderer maps it to its native interactive widget.

### `Renderer` ABC

Constructed with a `TransportCapabilities`. `dispatch(event)` routes each kind to the matching `on_*` handler and raises `ValueError` on an unknown kind. Handlers:

- `on_turn_start()` — default no-op, called once before the stream begins.
- `on_text_chunk(text)`, `on_thinking(text)` — abstract.
- `on_tool_call(tool_call_id, title, tool_kind="", tool_purpose="")` — abstract; mirrors native uniform tool-call semantics (each call marks the previous task complete and starts a new in-progress task).
- `on_prompt_choice(options, request_id, tool_title="", tool_purpose="")` — abstract; renders the interactive approval/choice prompt. The two tool fields ride the `PROMPT_CHOICE` event itself and are REDACTED like every other model-authored string. Both are defaulted, so an implementation that ignores them still satisfies the contract, but a renderer should PREFER them: the alternative is a name remembered from an earlier `TOOL_CALL`, which belongs to whichever call came last, so a permission request not immediately preceded by its own titled call names a different tool. Purpose is paired to title by `tool_call_id` rather than by recency, because the permission payload carries no purpose of its own and pairing by arrival order is what puts tool A's name beside tool B's purpose.
- `on_compaction(context_usage_pct)`, `on_done(stop_reason="")` — abstract.
- `on_steer_consumed(summary="")` — default no-op; Discord/Telegram seal the pre-steer segment and open the continuation with a native acknowledgement chip using the parsed summary, without receiving raw protocol text. Webex records the fold and notes it on the final answer instead, because a separate message per fold would bury the answer and it has no spare edit to spend on one.

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

`split_markdown_safe(text, limit, *, reserve=0) -> list[str]` is the shared markdown splitter every channel converges on. `chunk_text` above is blind fixed-width and the remaining per-channel splitters (Telegram's `_split_text`/`_split_markdown`, `slack/format.py::split_message`, the Webex and Weixin helpers) each carry their own fence handling, so a fix landed in one never reached the others. The module is stdlib-only and pure — no config objects, no modes.

Its contract:

- **Budget.** Every chunk is at most `limit - reserve` characters; `reserve` holds back capacity for a suffix the caller appends to each chunk. Empty text → `[]`. Text that already fits, a non-positive `limit`, and a `reserve` that consumes the whole budget → `[text]`. Lengths are Python `str` characters, not bytes or UTF-16 units. One documented exception: a logical line that admits no cut clean on both sides is placed **whole** rather than cut into a fabricated fence delimiter, whenever the **line itself** is no longer than `limit` — such a chunk holds that one line and nothing else. Eligibility measures the line alone, so the chunk adds its fence scaffolding (the reopener line, and the newline plus synthetic closer) on top and may pass `limit` by exactly that scaffolding; a chunk with no scaffolding to carry stays within `limit`. A bounded oversize is the accepted price of never fabricating a delimiter. Which placement a line takes is decided from the line and `limit` alone, before any budget arithmetic (remaining room, the reserved closer, what the chunk already holds) gets a say, so a cut without a clean boundary is reachable only for a line longer than `limit` — eligibility written as a guard along one arithmetic path is what made the ladder bypassable at budgets a fence's scaffolding consumes whole.
- **Real fence grammar.** An opener is ≤3 spaces of indent plus a run of ≥3 backticks or tildes (a backtick fence's info string may not contain a backtick); a closer is a run of the SAME character at least as long with nothing else on the line. Fence content is **opaque**, so a ``` line inside a ````diff block is content — the backtick-parity counters in the per-channel splitters get exactly this wrong and invert their open/closed state for the rest of the message.
- **Language-tag carry.** A cut inside a fence seals the chunk with a synthetic closer (same char, matching length) and reopens the next chunk with the original opener line, info string and indent included. The closer's cost is budgeted while inside a fence, so sealing can never push a chunk over — except after a whole-line placement, which deliberately does not reserve it (see Budget above).
- **Prefix stability (the streaming contract).** Splitting is greedy left-to-right and every cut depends only on the text BEFORE it, so re-splitting a longer prefix of the same stream reproduces every chunk except the last one byte-for-byte. A streaming caller sends sealed chunks as they appear and keeps only the final chunk as a live buffer. This outranks cut-quality heuristics: a nicer cut point that has to peek at the line after the cut is not allowed.
- **Cut preference.** Outside a fence: paragraph break if one sits at least halfway into the budget, else the last line break if it sits at least a quarter in, else a hard cut filling the budget (Discord's `limit//2` / `limit//4` ladder). Inside a fence: line boundaries only, hard-cutting a line only when it cannot fit a chunk at all. A hard cut splits one line across a chunk boundary, so **both** halves start a rendered line and the cut is pulled back until neither invents a fence: the prefix must leave the fence state untouched, and the remainder must not begin with indent or a fence character (judged by its first character alone — parsing a remainder that is still arriving would move an already-sealed cut). A fragment where every candidate lands inside a run admits no such cut, and the residue policy has two tiers: the line is placed **whole**, in a chunk holding it and nothing else, whenever the **line itself** is no longer than the caller's full `limit` — measured on the line alone, so the chunk carries its fence scaffolding on top of `limit` rather than cutting the line to make room (see Budget above); only a logical line longer than `limit`, undeliverable whole at any budget the caller allows, falls back to the widest prefix-clean cut, where the deferred remainder can still read as a delimiter. That last case is documented degradation in the same regime as an under-sized budget, and it now requires a single unbreakable line longer than the whole limit — not merely a no-clean-cut fragment that outgrew the current chunk, and not one whose fence scaffolding pushed the scaffolded sum over. The whole-line placement is reached by sealing the buffered chunk first, and that seal is keyed on cut cleanliness alone, never on the line's length: a seal driven by a line still arriving would move once the rest of it landed, rewriting a chunk already sent.
- **Whitespace.** Leading whitespace is never stripped (stripping it silently re-indents split code). Trailing whitespace is trimmed only when sealing outside a fence, where it cannot be content.
- **Tables.** A trailing pipe-bearing line is pushed to the next chunk when an earlier cut is nearby, which keeps a header row with its separator row; otherwise table lines are plain lines. Full table conversion stays with the per-channel renderers.
- **Termination.** Pathological input — a single unbreakable 10k-char line, a 5000-backtick run, a budget too small to hold a line's own fence scaffolding — terminates, at worst emitting over-budget chunks rather than spinning. Whole-line placement seals progress by consuming the line; the dirty-cut fallback keeps a width of at least one character. The **final** chunk of an unclosed fence is left open on purpose: callers own final presentation, and a streaming caller still holds it as a live buffer.

Discord is the first channel routed onto it, at two call sites, and it owns no fence grammar of its own: `discord/renderer.py::_rotate_on_length` consumes the streaming contract directly (seal every chunk but the last, retain the last as the live buffer, nothing appended to it and so nothing to strip back off), and `discord/session_resume.py::_replay_preview` takes the FIRST chunk as a bounded preview of a replayed transcript message, which is sealed and therefore closes any block the shortening opened. Both async call sites await `asyncio.to_thread(split_markdown_safe, …)`: the splitter terminates on pathological delimiter input, but its CPU work must not pause Discord heartbeats or unrelated turns on the event-loop thread. The remaining channels route on in follow-up changes; `test/test_messaging_split.py` pins each contract item above and `test/test_discord.py::TestRotationSplitting` pins the integration.

**A caller owes the bounded-overlimit case an answer.** The whole-line placement above can hand back a chunk over `limit` by its fence scaffolding, so a channel whose transport truncates silently must bound it again against the platform's own cap. Discord does: `_limit()` holds 100 characters back below the 2000-char cap, which absorbs ordinary scaffolding, and `renderer._fit_platform_cap` slices anything still over the cap at the single seal chokepoint. Blind fixed-width slicing is the right last resort there — it keeps every authored character at the price of a boundary Markdown may render badly, where `client.send_message`'s own truncation would drop the tail including the synthetic closer and give the user no signal. Session replay has a different tradeoff because it is only a preview: `_replay_preview` passes at most twice the delivery limit of redacted text to the prefix-stable splitter, uses the full redacted length to retain the truncation marker, and emits that marker alone when one pathological fence cannot fit with its closer. The canonical transcript remains untouched.

`iter_fence_spans(text) -> Iterator[tuple[int, int]]` is the same machine viewed over a whole message instead of one chunk's line fragments: it yields the character spans that lie inside a fenced block, opener line through closer line, with an unclosed fence running to the end. Both it and `split_markdown_safe` drive the module's single `_advance` state machine, so the open/close rule — which run length closes which fence character — exists once. A consumer that needs "is this offset inside code?" uses it rather than re-deriving the rule; the fence regexes stay private.

## Outbound file extraction (`outbound_files.py`)

An agent that produces an image writes it into the reply as markdown — `![chart](/tmp/chart.png)`. The dashboard renders that inline; a chat channel delivers the raw text, so the user reads a filesystem path where the picture should be. `extract_local_refs(text, *, limits=None) -> ExtractResult` pulls those references out for a transport to upload, and is the channel-neutral half of that: it decides which local references are safe to send, rewrites the text without them, and reports every refusal. The upload itself stays per-channel — each transport has its own multipart shape, per-file ceiling and count limit. `extract_local_refs_off_loop` is the async form; extraction reads files, so an async caller uses it rather than blocking the gateway's single loop.

`ExtractResult` carries `rewritten_text`, `files: list[OutboundFile]`, and `rejections: list[Rejection]`. An `OutboundFile` is `(path, data, alt, mime)` with `size_bytes` derived from `data`. A `Rejection` is `(dest, reason, detail)`, where `reason` is one of the module's `REASON_*` codes and `str()` renders the default prose. `ExtractLimits` sets the per-message budgets: `max_files` (references considered), `max_total_bytes` (aggregate, and the memory bound), and an optional `max_file_bytes` for a channel whose per-file ceiling sits below the aggregate.

Its contract:

- **Reference-bearing text reaches extraction before any splitter.** A caller may seal an ordinary prefix that ends before the earliest local reference, but it MUST hold the reference and its suffix intact for extraction; handing `![alt](path)` to a length splitter first can strand half a link in each chunk, unrecognisable to any later pass and visible as broken markdown. Extraction also shrinks the text that still needs final platform splitting.
- **Transports upload `OutboundFile.data` and MUST NOT re-open `path`.** Every gate below is applied to one inode, and a path resolved a second time at upload can name a different file by then — anything able to write that directory in between (another turn, a subagent, a cron) would substitute what gets sent. `path` is provenance: what a log line or a rejection names, and the raw material the sent filename is derived FROM.
- **The sent filename is derived, never the raw basename.** `upload_filename(file, index)` answers "what name is safe in a `Content-Disposition` header": the path came out of LLM-authored reply text, so only a sanitized basename survives, the extension follows the **sniffed** mime rather than the written suffix, and the RESULT is re-scanned so a name still shaped like a credential or a beacon URL is replaced outright. The extension half is the reachable one — extraction admits a file on its byte signature alone and never reconciles that with the suffix, so a raw basename can label a part with a type the bytes contradict, and the platform echoes that name back to everyone in the conversation. Discord, Telegram and Webex share `messaging/outbound_files.upload_filename`; Slack predates it and keeps an equivalent of its own in `slack/files.py`. That is duplication, not a gap — the two agree on every mime extraction can actually produce, since `_MIME_EXT` and `raster._MAGIC` have the same keys — and the shared one is where a channel added later goes.
- **A reference inside balanced inline code or a code fence is literal.** Inline spans reuse the length-preserving balanced-backtick masker used by rendered-block parsing; fenced offsets come from `iter_fence_spans` above, so neither grammar is re-derived here.
- **Only a real raster is sent.** Type comes from the leading bytes via `messaging/raster.py`, never an extension: a shell script named `.png` is refused, and SVG is scriptable markup with no signature. The same table decides inbound sniffing, so the two directions cannot disagree about a file.
- **The security floor is applied per reference**, because reply text is not trustworthy input — a prompt-injected agent chooses what it writes. Async channel extraction requires the acquired provider's actual `cwd` as its approved root; a path lexically outside that root is refused before metadata probes, and `safe_read_file_bytes_nolink(..., within_root=cwd)` rechecks the opened descriptor so a parent-symlink race cannot escape it. The existing `is_sensitive_path` denylist still applies; symlinks, hardlinks, and non-regular files are refused.
- **Every refusal is returned, never swallowed**, and the refused reference keeps its original markup so the path stays visible in the message. A file dropped in silence leaves a reply that talks about a picture with no picture and no explanation — the defect this module exists to prevent. This holds for a per-file-cap refusal too, so a channel with a low ceiling never has to drop an already-stripped file after the fact.
- **Caps bound work, not just output.** `max_files` counts references *examined*, so a reply full of unreadable paths cannot drive unbounded filesystem work or an unbounded rejection list. `max_total_bytes` is handed to the read itself, so an oversize file is refused rather than allocated, and `max_file_bytes` narrows that same read when a channel's ceiling is lower.

`test/test_outbound_files.py` pins each contract item above. Discord is the first channel routed onto it (below) and Microsoft Teams the second (see "Teams' file halves"); the remaining channels follow, and until one does its `files_outbound` stays `False` and it keeps printing paths. An adopter may be NARROWER than this module — Teams accepts only the raster subtypes it can render inline — but the one contract item it may not restate is the refusal: a reference this module accepted and the channel then cannot send must still be reported, and the path must still be visible.

`iter_local_refs(text) -> list[LocalRef]` is the scan both consumers share — every complete reference decidable from the text alone (inline-code and fenced ones, malformed markup and remote/`data:` destinations already excluded). `open_ref_start(text)` reports where markup OPENS and never closes, and `protected_ref_spans(text)` is the union of the two: the single answer to "where is image markup", used by the rotation guard and by `hide_local_refs(text) -> str`, the text-only cut a streaming channel uses to keep markup off live frames. An unterminated opener owns the rest of the text, because a buffer chunked while the reply is still arriving legitimately ends mid-markup — protecting only complete references is what lets a cut bisect `![alt](` and lose the attachment. `hide_local_refs` is deliberately more permissive than `extract_local_refs`: a reference it hides but extraction then rejects reappears in the sealed message, which is the safe direction; the reverse would flash a path and vanish.

### Discord's upload half (`discord/`)

The first channel wired onto the module, and the shape the others follow:

- **Named ceilings fed in as budgets, on a multipart path that shares the JSON ladder.** `client.py` declares `DISCORD_MAX_FILE_BYTES` (10 MiB), `DISCORD_MAX_FILES_PER_MESSAGE` (10) and `DISCORD_MAX_TOTAL_UPLOAD_BYTES` (25 MiB — Discord's own total is below files × per-file, so the aggregate is what bounds the bytes one seal holds); the renderer turns them into `ExtractLimits`, so an oversize file is refused *by the read* and keeps its markup instead of being uploaded and 413'd, or dropped after its reference was already cut out. `_api_multipart` sits beside `_api` and both run through one `_api_request`, so the 429 back-off, the non-JSON-body degradation and the transport-error logging exist once. The body is rebuilt per attempt because an aiohttp form is consumed as it is written — replaying one sends an empty body. `payload_json` leads, then one `files[N]` part each, with an `attachments` descriptor list built where the parts are so a descriptor's `id` always names its own part.
- **Only semantic seals extract, once.** Before any length rotation, the earliest complete or still-arriving local reference and its suffix stay in the live tail; the preceding ordinary text may seal through the shared splitter, but length-sealed chunks never run extraction. The semantic steer/final seal therefore sees the reference atomically in its original whole-text fence context and uploads each file exactly once. The shared splitter documents one context-degrading tier, reachable only for a logical line longer than the full limit; if that tier is entered before a later image appears, the segment remains upload-ineligible and its markup stays literal. Both the protected-span scan on rotation and `hide_local_refs` on live frames run off-loop; neither can starve the gateway on adversarial markup. An image-only reply ships as an attachment with no raw path.
- **A failed upload restores display-redacted markup.** Discord takes every file in one multipart call, so failure is all-or-nothing. Before fallback splitting or JSON sends, the original segment runs through display-form redaction; ordinary safe image markup is restored verbatim, while markup that concealed a credential may intentionally lose formatting to keep the rendered secret redacted. Recovery splits against Discord's real `DISCORD_MAX_TEXT` ceiling with the shared splitter, then applies the hard-cap fallback for its documented scaffolding exception, so authored tails are never silently truncated.
- **Descriptions, filenames, and transformed body text are separate sinks.** Extraction unescapes alt text, so descriptions are re-scanned with the exfiltration and credential pair across both literal and canonical display forms before truncation. Filenames keep only a sanitized basename and normalize the extension to the sniffed type. Removing image markup can also reassemble a credential through Markdown that Discord hides; the transformed body therefore scans both its invisible-character-normalized literal form and canonical display form with both redactors before selective mention neutralization. The literal pass keeps a retained/rejected image destination visible to the scanner even when link canonicalization would remove it.
- **Two gates, both leaving the text untouched when they refuse, and every refusal is audited.** `files_outbound` is read before extracting, so a channel without an upload path keeps printing the path rather than silently dropping the picture. The second is the restricted-session ceiling: an approved guild thread is readable by every member who can view it, so a session the user expected to leave no trace must not ship bytes into one. A LIVE dashboard slot answers off the same `slot.is_restricted` signal that denies artifact registration; when the tab has been ARCHIVED the slot and its restricted key are both gone while the mirror binding persists, so the gate resolves the transcript's own `memory_mode` off-loop through `_probe_persisted_session` — which REFUSES to answer when one stem matches several transcripts, since taking the first candidate would let a legacy persistent file answer for an incognito session — and denies on restricted, ambiguous OR unreadable. A key that is not `dashboard:` never had a slot, so the slot rungs cannot answer for it — the CHANNEL's own privacy mode does, on the same `is_restricted` predicate its transcript, memory and title writes use, so one conversation cannot be private for three of them and public for the fourth. A flat allow there was correct only while no channel-native conversation had a privacy mode; it became a hole the moment Telegram gained `/temporary`. Still not a blanket fail-closed: an unrestricted conversation is allowed, which is the common case, and a channel offering no modes reads exactly as before. Restricted-session denials use `discord_dispatch.upload_files`; extraction refusals use `discord_renderer.upload_files` with only their closed reason codes and counts, never the LLM-authored destination.

## Per-target table rendering (`tables.py`)

A GFM pipe table is unreadable on a channel that renders Markdown but not tables: the pipes arrive literally and every column wraps, so on a phone a three-column table becomes a wall of ragged text. `render_tables(text, *, policy, native_tables=False, final=True)` converts it into something the channel can render. Stdlib-only and pure, like `split.py`.

**It is an OUTBOUND presentation transform, not a rewrite of the turn.** The canonical assistant text — what `TurnDriver.run` returns, and therefore what the transcript, history and the dashboard show — never passes through it. A renderer applies it to the bytes it is about to hand its platform client, so the same turn keeps pipes on the dashboard and gets cards on Discord. Each channel's `text()`/`_segment_text()` accessor stays canonical for the same reason: those also feed history.

**Policy is per delivery TARGET, never per session.** `TransportCapabilities.table_mode` carries the target's declaration and `Renderer.render_tables_for_target()` resolves it against `TransportCapabilities.native_tables`, so a session mirrored to two channels can send pipes to one and cards to the other.

| Policy | Meaning |
|---|---|
| `off` | No conversion. The floor, so a channel that never opts in is byte-unchanged |
| `cards` | One card per row: first column as a bold heading, later headers as labels |
| `grid` | An aligned monospace grid inside a fenced code block |
| `native` | The target renders tables itself, so pass through — **but only when it really does** |
| `auto` | Per table: a grid while its DISPLAY width fits `GRID_MAX_DISPLAY_COLUMNS` (42), cards once it does not |

`resolve_table_policy(policy, native_tables=…)` is the capability check, and it exists for one case: **`native` on a target declaring `native_tables=False` resolves to `cards`, never to raw pipes.** `auto` on a native target resolves to `off` (the platform's own table beats anything rendered here); explicit `cards`/`grid` are operator intent and are honoured on any target; an unknown policy normalizes to `auto` rather than `off`, because the unsafe fallback is the one that ships pipes.

Per-channel declarations:

| Channel | `native_tables` | `table_mode` | Why |
|---|---|---|---|
| Discord, Teams, Webex | `False` | `auto` | Markdown but no table rendering; delivery can split safe cards across messages |
| WeCom | `False` | `off` | One replacement bubble has no continuation path, so no adaptive form can guarantee both display safety and complete delivery under its hard cap |
| Telegram | `True` | `native` | `sendRichMessage` renders a real table, and `_seal_table_fallback` monospaces the run when that path is unavailable |
| Weixin | `True` | `native` | iLink renders Markdown natively, which `weixin/renderer.py` deliberately preserves |
| Slack | `False` | `off` | Renders no table, but `slack/format.py::_convert_tables` already flattens on the render path and the golden-transcript harness pins those bytes |

Contract details:

- **Display width, not `len`.** `display_width` counts East Asian `W`/`F` as two columns and combining marks and zero-width characters as none. Padding by `len` produces a grid whose columns visibly step, and thresholding by `len` sends a CJK table twice the viewport width down the grid path.
- **Conservative.** Anything not unambiguously a GFM table is left byte-for-byte alone: prose, a pipe-bearing sentence, a line indented by 4+ display columns with spaces or tab expansion (that is code), a malformed table whose separator row's cell count does not match its header's, a Markdown block starter in any table position (including a dash-list item that otherwise resembles a separator), anything inside a real fenced code block, and every CommonMark raw HTML block. HTML block contents are opaque until their specified closing marker or blank-line boundary, so table-shaped source inside `<pre>`, comments, declarations, block tags, and complete custom tags is never rewritten. Fences are line-anchored per CommonMark (backtick or tilde, indent ≤3, closer run ≥ opener's, a backtick fence's info string may not contain a backtick) and their content is opaque, so a ``` inside a ````diff block is content. A fence opened after a bullet or ordered-list marker carries its container indentation through the closing delimiter, so table-shaped code in the list item remains opaque and a table after the item can still convert.
- **Post-transform safety.** When conversion changes outbound text, `Renderer.render_tables_for_target()` re-runs credential and exfiltration redaction against the display form before delivery. Cards join headers and values that `TurnDriver` scanned on separate table lines; the second pass prevents a generated `Authorization: Bearer …` label/value line, or formatting removed by the platform, from assembling a secret after the channel-neutral pass. Delivery-framing fallbacks pass an explicit `cards` policy back through this same helper; Discord, Teams and Webex never call the pure formatter directly after deciding an adaptive grid crosses their cap. `off`/supported-`native` output that did not change remains byte-identical.
- **Protocol remains canonical.** Discord keeps authored source in its protocol buffer and table-rendered text in a separate delivery snapshot. Steering rotation and trailing `[OPTIONS: …]` extraction read only canonical source; steering directives are single-line and cannot span table rows. Both final and pre-steer seals consume canonical options before rendering, and presentation output is used only for streaming, size checks, and delivery—marker-shaped card content is never reinterpreted as controls.
- **Cell boundaries.** During rendering, a pipe is content, not a boundary, when it is `\|` (decided by walking the row, so `\\|` stays a boundary) or inside an inline code span. The code-span rule is deliberately LOOSER than GFM, which would split there and leave the backticks unpaired: the decision here is only a rendering, and the alternative is silently deleting a pipe the author wrote. Shape validation remains strict GFM and counts every unescaped header pipe, including one inside a code span, so malformed header/separator widths stay byte-identical. Conversion also requires that strict GFM width to equal the lossless rendering parser's header width; an ambiguous header stays byte-identical rather than shifting or truncating body cells. An unpaired backtick opens nothing.
- **Cards preserve empty data.** The first body cell becomes a bold heading only when that cell has a value. A blank repeated-category cell stays blank while the row's remaining labeled values render; the column header is never substituted as invented row data. Empty non-heading cells are omitted rather than emitted as bare labels.
- **`[OPTIONS:` / `[OPTION-ACTIONS:` / `[STEERING` lines terminate a run.** All three carry pipes and are emitted directly under an answer, so a table one line above would otherwise swallow the trailer as a body row and render the user's choices as a card. An action body is exactly that shape (`close=Ship it | close=Hold`). The heads come from `constants.MARKER_PREFIXES` via `constants.starts_with_marker_head`, not a literal list here, so adding a head reaches this scan — and the match is cased PER HEAD, so a lowercase `[option-actions:` line terminates a run just as the parser strips it. `[OPTIONS:` stays case-SENSITIVE, a pre-existing divergence from the frontend that is recorded rather than widened.
- **Whitespace and containers are preserved.** Whitespace outside a converted run remains exact, including blank-line runs and a trailing newline. A run's leading container indentation (up to three display columns) is carried onto every nonblank rendered line, so conversion does not pull a nested table out of its indented list context.
- **Grid fences cannot collide with cell content.** The generated backtick fence is longer than every backtick run in the aligned header and body, so a literal ``` cell remains content rather than closing the grid.
- **Idempotent.** Neither rendering contains a table run (cards carry no pipes; a grid lives inside a fence, which is never entered), so a streaming renderer may convert its buffer eagerly and re-convert what it retains.
- **Streaming (`final=False`).** A table run reaching the end of the text may still be growing, so it is left raw; the same deferral applies when the immediately following final line is unterminated, because `Row ` can become the outer-pipe-less body row `Row 1 | ok` in the next chunk. Converting either state would freeze a half-arrived row as a card and strand the rows behind it. A run terminated by settled real content converts either way. Discord treats the difference between the streaming and final render as a pending-table signal: once the header and separator are recognizable it buffers that segment and neither rotates nor updates the live frame until prose terminates the run or the turn finishes. A separator split across provider chunks may therefore leave one earlier partial-pipe frame visible briefly, but the completed send edits that same message to the rendered table; no recognized table is split into raw rows.
- **Degenerate cases.** A header-only run renders as a grid regardless of policy (there is no row to make a card from, and dropping it would lose the run's only content). The same lossless fallback applies when a nonempty body's sparse rows produce no card lines: the grid preserves both headers and empty cells instead of replacing the table with nothing.

Discord converts INTO its buffer rather than at the send seam, because `_rotate_on_length` sizes messages from that buffer and cards are longer than the pipes they replace; converting on the way out could seal a message past the platform's hard 2000-char cap. A still-growing trailing table remains buffered even when it grows beyond one message, then the completed output is split normally — no rotation may strand headerless raw rows. When a generated grid itself exceeds one Discord segment, Discord first re-renders it as cards: the shared splitter reopens a cut fence with its original opener, so a split grid stays valid Markdown but its header row reaches only the first message, while cards stay readable on their own. Teams and Webex likewise re-render a narrow grid as independently readable cards before their normal character/byte chunking when it would cross one platform message. If forced cards retain an over-cap grid, those split-capable targets preserve display-safe raw table text through ordinary continuation chunking instead; generated-grid metadata prevents this fallback from downgrading valid cards. WeCom keeps canonical table text because its streamed answer has no continuation-bubble path: a transformed form can exceed the hard cap while the only shorter raw candidate still contains a value that display-form redaction would remove.

`test/test_messaging_tables.py` pins the pure contract; `test/test_channel_table_rendering.py` pins which target converts, that the driver's canonical text does not, and the `native`-without-the-capability coercion.
## Turn-status surfacing (`status_reactions.py`)

Two things tell a chat user how their turn is going without adding a message to
the conversation: a reaction on their own message that swaps as the agent moves
through the turn, and one low-emphasis line at the end carrying how long the turn
took and how full the context window now is. Both live in this one module because
both are the same shape, per-channel decoration over channel-neutral state, and a
channel that keeps its own copy of either drifts from the rest.

**The phase ladder.** `PHASES` is the ordered vocabulary everything here is keyed
on: `queued`, `thinking`, `coding`, `browsing`, `tool`, `done`, `error`.
`PhaseReactionLadder.set_phase(phase)` walks it, and exactly ONE reaction
represents the turn at a time: entering a phase removes the previous emoji and
adds the new one. Three tiers decide when a transition reaches the channel.

- `TERMINAL_PHASES` (`done`, `error`) land at once and end the ladder. `finalize`
  is idempotent and clears any stall mark BEFORE the terminal emoji, since a
  leftover mark beside it reads as still-stuck.
- `IMMEDIATE_PHASES` (`queued`) also land at once: that one is the receipt for
  "your message arrived", and delaying it defeats the point of it.
- Everything else is **debounced** by `LadderTimings.debounce`
  (`DEFAULT_DEBOUNCE_SECS`, 0.7s), re-armed on each transition, so a turn that
  fires five tools in a second costs one visible swap instead of five. Every
  channel rate-limits reactions, which is what the debounce protects: a tool burst
  must not spend that budget on emoji nobody reads.

A stall watchdog adds a SECOND, additive mark once nothing has happened for
`stall_soft` (15s) and upgrades it at `stall_hard` (45s, the hard mark replacing
the soft one rather than joining it), so a wedged turn is visible without the user
asking. `on_progress()` resets it on any activity, and
`pause_stall_watchdog()` / `resume_stall_watchdog()` bracket a legitimate wait on
a human, which is why a turn parked on an interactive tool approval never earns
the "gone quiet" mark. A `StallEmojis` field left `None` suppresses that mark and
its timer, so a channel with no stall vocabulary schedules no watchdog at all. All
four durations sit on the injectable `LadderTimings`, so a test needs no real
clock.

Classification is `tool_to_phase(name, kind)`. A declared ACP tool kind wins over
the name, because a bring-your-own tool can be named anything while still
declaring a known kind, and an MCP tool's fully qualified name
(`mcp__example-mcp__Bash`) reduces to its base name after `__`; anything
unrecognized is the generic `tool` phase. `phase_for_tool_title` is the same
answer for a DISPLAY title: kiro-cli titles a command tool `Running: <cmd>`, so a
channel handing the title straight over would classify every command as a generic
tool.

**The emoji sink is injected, never imported.** The single channel operation the
ladder needs is "add or remove ONE emoji on ONE message", and it arrives as a
`ReactionSink` bound to one message (`CallableReactionSink` wraps two coroutine
functions, so a channel binds its client, channel and message into closures at the
call site rather than writing a sink class). That is what keeps the pinned one-way
dependency direction intact: `kiro_crew.messaging` imports nothing from
`kiro_crew.slack` / `kiro_crew.discord` / `kiro_crew.dashboard`, and a ladder that
reached for a channel's REST client would reintroduce exactly the cycle this
package exists to remove. The ladder therefore never handles a channel id, a
message id, or an emoji vocabulary. Either sink call may fail, and a failure is
logged at debug and dropped: a reaction is decoration, so a deleted message, a
revoked scope, or a rate limit the channel refused to queue must not surface as a
failed turn.

**The phase-to-emoji table is per channel, for the same reason.** Emoji are the
channel's vocabulary: Slack's reaction API takes a shortcode (`eyes`), Discord's
route takes the unicode character itself. So the table is injected alongside the
sink, and `merge_phase_emojis(defaults, overrides)` applies a user's overrides onto
whichever table the channel owns, RETURNING the unknown keys rather than honouring
them so the caller can report a typo instead of dropping it in silence. A `None`
value suppresses that phase (no emoji is added, though a transition into it still
clears what the previous phase left behind), and a phase with no entry falls back
to its own name so a channel that adds a phase still shows something.

**`close()` drains, and that is the whole point of it.** Both halves of the ladder
outlive any single `await`: the debounce and stall timers are `loop.call_later`
handles, and every sink call runs as a task the ladder holds a reference to so the
loop cannot collect one mid-flight. `close()` cancels the timers FIRST so nothing
new is scheduled, then gives the calls already in flight one bounded window
(`close_drain`, 5s) to land before cancelling them and gathering the results: they
are the turn's last emoji and worth waiting for, but a channel API that never
answers must not hold teardown open. Skipping either half is the defect this
prevents. An armed timer fires against a turn that has already finished,
re-decorating a message nobody is waiting on; an untracked task outlives it, so
a renderer torn down by an exception leaks work into whatever runs next. `_spawn`
closes rather than starts a coroutine once the ladder is closed, for the same
reason.

**The turn-end line** is the module's other half and carries no ladder state.
`format_turn_status(elapsed, context_pct)` renders `Finished in 3m 7s` plus a
context-usage chip banded by how close the window is to needing a compaction
(green below 30%, yellow from 30%, orange from 50%, red from 70%), and omits it
entirely when the
caller could not read a usage figure: absent means unknown, which is not 0% and
must not render as a reassuring green.

Discord is the first channel on the shared ladder: `discord/renderer.py` owns
`DISCORD_PHASE_EMOJIS` (unicode rather than shortcodes, one code point each,
because the reaction route carries the emoji as a percent-encoded path segment),
`DISCORD_STALL_EMOJIS`, and a
`_MessageReactionSink` bound to the user's message whose PUT/DELETE both route
through the client's request ladder so they share its rate-limit accounting. The
ladder is armed lazily and only when there is something to decorate: reactions
enabled for the channel, a transport that declares `reactions`, a recorded user
message id, and a renderer that has not been closed, since a late event after
teardown must not arm a fresh ladder whose timers would then outlive the turn.
Slack still runs its own `StatusReactionController` in `slack/handler.py`, the
implementation this module was generalized from, so until Slack moves over the
same phase machine exists twice. `test/test_status_reactions.py` pins both: the
shared ladder's phases, debounce, stall marks, close-drain and sink-failure
tolerance, and Slack's controller beside them.

## Layer 3 — session-key namespacing (`link.py`)

Session keys are namespaced as `f"{channel_type}:{conversation_id}"` (`session_key()`) so keys never collide across channels (`SLACK_NAMESPACE = "slack"`). Legacy native-Slack sessions were keyed by the bare `thread_ts`; helpers provide the bidirectional `bare ⇄ slack:` shim consumed by `SessionMap` (`session_map.py` imports `ChannelLink` + `canonical_key`, no import cycle):

- `is_legacy_slack_key(key)` — True iff `key` is a bare Slack `thread_ts` (matched by `_SLACK_TS_RE = r"\d+\.\d+"`, digits + one dot).
- `canonical_key(key)` — normalizes a bare legacy key to `slack:<thread>`; non-legacy keys (`dashboard:`, `channel:`, `slack:`, …) pass through unchanged. `SessionMap._load` (called from `__init__`) migrates bare keys and populates a Layer-3 `ChannelLink`; `get()`/`set()` re-canonicalize so a not-yet-updated caller passing a bare `thread_ts` still resolves.
- `legacy_key(key)` — returns the bare `thread_ts` for a `slack:<thread>` key, else `None`.

`ChannelLink(channel_type, channel_id=None, thread_id=None)` records the inbound channel a session belongs to (its **own** channel), with `to_dict()`/`from_dict()`. It is deliberately distinct from the dashboard→Slack *mirror* binding, which stays behind `SessionMap.get/set_slack_link` and is **not** modeled here (guardrail G3).

## Config flag & routing

`MessagingConfig.use_transport` (`config/loader.py`, default `True` in Kiro Crew; exposed in `config.json` under `messaging`) is the single switch. `slack/events.py::_route_message` checks `orch._cfg.messaging.use_transport`; when `True` it creates a task on `handle_message_transport` and skips the native `handle_message` monolith. (There is no challenge-redirect in this fork — Slack messages are processed inline.) Approval mode is resolved by `_resolve_approval_mode(orch)` (respects configured mode + operator YOLO/SafetyOverride TTL), and the per-channel `slack.channels.<id>.agent` override is passed through.

## Proactive sends (`send_message`'s `channel_type`)

Everything above carries a REPLY: an inbound message arrives, a turn runs, its
output goes back where it came from. A proactive send has no inbound message to
answer — a silent cron finishing, a background task reporting — so it has to name
its destination, and for most of this module's life the only names it had were
Slack's. An agent driven from Telegram could not notify its own operator at all,
and a silent cron on such a session reported nowhere: not to Slack (no thread), not
to the dashboard chat (no slot), only to the bell nobody on Telegram sees.

`send_message`'s `channel_type` closes that. It is one of **two mutually
exclusive destination families**, and a call carrying both is refused rather than
resolved by precedence — either order silently drops a destination the caller
named, and the caller cannot tell which one it lost:

| Family | Fields | Reaches |
|---|---|---|
| Slack | `session="slack"`, `channel`, `user`, `thread_ts`, `reply_broadcast` | the Slack client's own path |
| every other channel | `channel_type` | the conversation the calling session already belongs to |
| every other channel | `session="<channel>"` | that channel's own configured owner, wherever the call came from |

The two non-Slack rows answer different questions and neither substitutes for the
other. `channel_type` rides the calling session's `ChannelLink` and **fails closed
when there is none**, so it serves a session that is already talking on the
channel; a cron whose origin is the dashboard, or which has no origin at all, has
no link for it to ride. `session="<channel>"` needs no link — it resolves the
destination from the transport's own `configured_targets()` through
`_deliver_channel_dm` — which is what makes it the route for a proactive notice
raised somewhere other than the channel itself.

**One roster, three views.** The channels a proactive send may name are derived
ONCE, as `constants.CHANNEL_SEND_NAMESPACES` — `CHANNEL_SESSION_NAMESPACES` minus
`slack` (its own client, spelled `session="slack"`) and `unified` (a session-key
bucket, not a transport). The MCP tool's advertised enum
(`mcp_tools.messaging._CHANNEL_SESSIONS`), the pre-handler validator's pattern
(`validation._SEND_MESSAGE_SESSION_RE`) and the gateway's accepted `channel_type`
set (`_SEND_MESSAGE_CHANNEL_TYPES`) all read that one constant, because each is a
gate the value passes in turn and a narrower one in front silently withholds a
destination the wider ones behind it would serve. That is not hypothetical: the
first two were hand-kept and stood at `discord` alone for months after eight more
transports were registered, so `session="webex"` was refused as malformed by a
validator while the gateway leg behind it could already resolve and deliver a
Webex owner DM (#6514). The subtraction itself was then respelled at each of the
three readers, which is the same shape one level up, so it is derived at the
definition and the readers name it rather than recompute it.

The roster lives in `constants` (`os` + `re` only) rather than `messaging.link`
because it has readers on both sides of an import cycle: `messaging.link` is
itself stdlib-only, but importing a name FROM it executes `messaging/__init__.py`,
which pulls in `driver` → `acp` → `hooks`, and `hooks` → `webhooks` →
`validation` is already an edge. `messaging.link` re-exports both names, so its
own readers are unchanged. One sibling hand-kept copy remains and is NOT derived:
`autonudge._CHANNEL_KEY_PREFIXES`, which answers a key-SHAPE question rather than
a send-capability one. Its membership is currently identical to the roster's, so
the older claim that it is deliberately narrower does not hold; deriving it is
sound and deliberately out of scope here.

**A channel `session` needs an owner; `channel_type` does not, so the rosters
differ by one.** `channel_type` names a conversation -- the calling session's, or an
explicit `target_id` -- and infers no recipient. A channel `session` infers one via
`_owner_dm_target`, whose safety claim is that the agent can only reach somebody the
USER configured. `constants.CHANNEL_OWNER_DM_NAMESPACES` is therefore
`CHANNEL_SEND_NAMESPACES` minus the channels that cannot answer that question, and
today that is `weixin` and `wecom`. Each folds identities LEARNED from inbound
traffic into `configured_targets()` — Weixin's `_known_users` (`_allowed |
_known_users`), WeCom's `_warm_chats`, which under `allow_all_users` become the list
outright ("there is no configured list to draw on, so the warm peers ARE the list").
So a peer who messaged the bot once can be the single available direct target, which
is exactly what `_owner_dm_target` reads as the owner. Nothing downstream catches
it: both `may_send_to` implementations return True unconditionally under their open
policy (Weixin's promise to consult `_allowed` alone holds only on its `allowlist`
branch), and `resolve_configured_target` accepts the learned set too.
`channel_type` is unaffected for both and stays available.

The exclusion is not hand-kept:
`test_no_owner_dm_channel_advertises_learned_identities` derives the check from the
transports, so a channel that starts mixing learned identities into
`configured_targets()` fails that gate instead of silently becoming an owner-DM
target. A channel graduates by separating configured recipients from learned peers,
at which point deleting it from the subtraction is the whole change.

**Widening the roster does not widen the audience.** Whether a channel can take a
proactive DM is answered per send by `_owner_dm_target`, at the side-effect
boundary against live config, which is where a runtime question has to be answered
— a static roster cannot. A transport needing prior inbound state (Teams, WeCom)
or unable to DM proactively at all (Feishu) marks its targets unavailable; an
allow-list with several people is refused rather than guessed at. Both degrade to
the dashboard notification, and the tool says which channel missed rather than
reporting success.

**Both channel legs require a strictly-resolved caller.** `channel_type` and a
channel `session` each resolve identity through `mcp_core.require_strict_session_key`
and refuse when it is absent. The refusal is the control, not a formality:
`gov_session` otherwise falls back to the lenient resolver, which walks process
ancestors, so an unidentified sub-agent resolves to its PARENT — and the
channel-agent containment check (`_deny_channel_agent_messaging`, which fires only
on an identity starting `channel:`) then does not fire for a contained agent,
putting the owner-DM egress surface inside its confinement boundary. The gateway's
fail-closed `channels` re-vet does not backstop that: it covers the transport
scope, not channel-agent containment. Refusing costs no legitimate caller, because
the gateway injects `KIROCREW_SESSION_KEY` into every agent/ACP subprocess and a
cron run carries `cron:<job_id>` in it, with the HMAC-verified host-pid sidecar
covering PID-namespace-sandboxed sessions; it is absent only where there is no
session key to inject.

**And the gateway vets that identity rather than the host sentinel.**
`_deliver_channel_dm` resolves `caller_session or HOST_SESSION_KEY`, and the host
sentinel is the PERMISSIVE default, so dropping a caller's identity there lets a
session whose own profile denies the transport reach a host-permitted owner DM —
the MCP-side channel vet degrades open on an evaluation error, so it is not a
second guard. Each arm takes its identity from where that arm is trustworthy,
matching `_channel_delivery_key` on the same path: a cron's body key is
format-validated before it may escalate routing, and a non-cron caller is
identified by the `X-Session-Key` header, which `token_auth._verify_unix_peer`
kernel-attests against the peer's own process ancestry — never the body, which a
tool argument could name. The governance vet and the delivery key therefore
resolve the same principal. The `channel_type`/`target_id` leg already worked this
way; the channel-`session` leg was not migrated with it.

`"slack"` is deliberately **not** a legal `channel_type`: Slack is absent from
`state.channel_transports` on purpose, so the shared ladder skips it and accepting
the value would fail every such send closed with no useful reason.

**The delivery ladder**, in `dashboard/handlers/messaging.py::api_send_message`:
origin session injection (`session="origin"`, unchanged) → `_deliver_to_channel`
→ Slack. `_deliver_to_channel` rides
`chat_runner._resolve_channel_target`, the same governed cross-surface seam as the
outbound mirror, the auto-compact notice and the inbound-unbind notice, so a
proactive send is capability-checked, `channels`-vetted (fail-closed) and
SEL-audited exactly like every other outbound notice rather than through a
second, differently-gated copy. Its text goes out through
`renderer.display_safe` — the shared outbound display sink, redaction against the
RENDERED form and then mention defang — so this egress cannot be sanitised
differently from the ones beside it, and it adds no new sink to
`security_posture._REDACTION_SINKS`.

Four properties are load-bearing:

- **A named channel is not a Slack fallback.** `channel_type` suppresses the
  Slack leg outright, including the cron-to-owner-DM default. A failed channel
  delivery falling through to Slack would post the message to an audience the
  caller never named, which is worse than not delivering it.
- **Failure is reported, not absorbed.** Every refusal — no link, a link on
  another transport, a governance denial, an unregistered transport, one that
  cannot send proactively, a transport error — returns `False`, is audited with
  its reason, and surfaces as HTTP 502 `channel_delivery_failed`. The bell is not
  a substitute for the surface the user is reading, so a notification-only
  outcome must not read as success. `delivered_to` gained a fourth value,
  `"channel"`, for the same reason: without it a successful Telegram send still
  reported the "reached the dashboard notification only" warning.
- **The caller does not get to name the conversation.** A cron's destination
  comes from its job's stored `session_key` (gateway-owned scheduler state);
  every other caller is identified by the `X-Session-Key` header, which
  `token_auth._verify_unix_peer` kernel-attests against the peer's own process
  ancestry. The request BODY is never consulted for this, because nothing checks
  it — a body naming another session's key would post into a conversation the
  caller does not own. Note this is a different key than
  `_resolve_session_target` returns: that one strips `dashboard:` to get a slot
  NAME, while channel links are keyed by the full session key.
- **The governance gate names the real transport.** The `channels` scope is a
  per-transport allowlist, so the MCP tool vets `channel_type`'s transport and
  not `"slack"`. Vetting the wrong one evaluates a Telegram denial against
  Slack's rule, and refuses a permitted Telegram send whenever Slack is denied.
- **Revocation is re-decided at egress, not trusted from the link.**
  `_resolve_channel_target` consults `transport.may_send_to(link.channel_id,
  link.thread_id)` after the governance gate and refuses the target when it says
  no, so **every** proactive leg inherits the check rather than each remembering
  it. The two gates answer different questions: governance asks "may this session
  use the telegram channel at all", which stays permitted, while this asks "is
  this conversation's recipient still on the roster". A denial is SEL-audited
  (`operation="channel.proactive_send_authorize"`, `outcome="denied"`) because a
  revoked recipient silently losing its notices looks exactly like an idle agent,
  and a raising implementation fails **closed**, because an allow-list check that
  errored has authorized nobody.

  The check gets two inputs, because one alone cannot serve every channel. The
  **conversation id** answers it wherever that id already IS the roster identity:
  **Telegram** (a private `chat_id` IS the `user_id`; a Topic routes through the
  same `forum_gate_outcome` predicate as inbound, so outbound can never reach a
  Topic inbound would refuse), **iMessage** (the handle IS the conversation,
  normalized both sides), **Weixin** (mirrors its `dm_policy`, and deliberately
  ignores the learned `_known_users` set so a peer who spoke once cannot outlive
  its removal) and **WeCom** (defence in depth; it declares
  `supports_proactive_send=False`, so the ladder refuses it earlier). **Teams**
  reverse-maps through `_reachable_conversation`, the same predicate
  `resolve_configured_target` and `configured_targets` answer from, so "may I send
  here" cannot drift from "where do I send". Its `ServiceUrlStore` is PERSISTED, so
  it permits while that store is still unread and enforces from the first load
  onward: `send_message` awaits its own `ensure_loaded`, and the transport is
  registered before `connect` starts the warm-up, so denying in that window would
  refuse a send the transport can complete from a route already on disk.

  The **principal** covers the rest. `chat_runner._session_principal` recovers the
  peer's platform id from the session key, whose canonical grammar is
  `{surface}:{agent}:{chat_type}:{scope…}` with the scope of a 1:1 DM being exactly
  that peer, using `messaging.link.parse_session_key` because that module is the
  one canonical address parser. This is what makes **Discord** (a DM link persists
  a channel id unrelated to the user snowflake, and re-deriving it is a POST a
  synchronous seam cannot make) and **Webex** (binds a `room_id` while the roster
  holds emails) able to reach their rosters at all, so a revoked DM recipient is
  now refused on those channels too. Both are the principal answer for their **DM**
  route only: each also owns a room-audience roster — Discord's `_allowed_threads`,
  Webex's `_allowed_rooms` — and answers that route from it instead, because a room
  route has no principal to name (see below).

  It is deliberately empty rather than wrong whenever the key names no single
  person: a forum/group route scopes to `(chat_id, thread_id)` so its audience is a
  room, a `unified` DM bucket drops channel and user out of the key by design, and
  a legacy key does not parse. Empty means "the key does not name one principal",
  never "nobody is authorized".

  **It is derived from the KEY alone, and that is a security property rather than a
  convenience.** Two other records name a peer and neither is usable, because a
  principal only authorizes anything if it describes the conversation the link
  points at. The session's stored channel value (`{namespace}:{user_id}`) is written
  ONCE at session creation while the origin/mirror link is rewritten on later turns,
  so under a `unified` bucket, which collapses several peers' DMs into one session on
  purpose, the two drift: the attribution can name the peer who created the session
  while the link points at a different peer's conversation. Authorizing against it
  would check the wrong person and **pass**, which is worse than declining to name
  one. A forum scope's `scope[0]` is a supergroup id, and its stored value is the
  last sender rather than the audience, for the same reason.

  **A transport with more than one audience dispatches on the route rather than
  testing one id against the wrong roster.** Discord has two: a **thread** route
  (identified by `thread_id`, which `receive` sets to the thread's own snowflake,
  the same value it uses as the conversation id, and leaves unset for a DM) is
  checked against `_allowed_threads`, the set `receive` itself gates inbound on,
  so outbound is exactly as tight as inbound. That also settles the auto-created
  thread: those ids are registered in memory only, so after a restart such a thread
  cannot drive a turn either, and continuing to post into it would make outbound
  the more permissive of the two. A **DM** route is checked against `_allowed` via
  the principal.

  **Webex** has the same two, and a group space is why: a **space** route is
  recognised by its conversation id being in `_allowed_rooms` while
  `allow_group_rooms` is still on — the same pair `room_permitted` gates inbound on,
  so outbound is neither tighter nor looser. Both sets are frozen at construction
  and the config PATCH reports `restart_required` for them, so flipping the switch
  off or dropping a room revokes that space's proactive traffic from the next start —
  which is what matters, because a persisted link outlives the config that
  authorized it. This arm is load-bearing rather than a convenience: a space is keyed as a
  `forum` route on `(chat_id, thread_id)`, so `_session_principal` names nobody by
  design, and answering only via the principal would drop **every** proactive send
  into an allow-listed space while the in-channel turn path kept working — a space
  that answers an `@mention` but is silent for the dashboard mirror, cron results,
  subagent completions and compaction notices, which reads as half-alive rather than
  misconfigured. A **DM** route is checked against the email allow-list via the
  principal, because a session binds `room_id` while that roster holds emails and
  nothing in the process maps one back to the other.

  A DM route with no principal, which means a `unified` bucket, is **refused** on
  those two transports: neither roster can be consulted, and an unidentifiable
  recipient at a network egress boundary must not be posted to. That costs an
  unattended notice on unified-scope Discord and Webex sessions, and it is the
  correct trade rather than a regression to accept, because that bucket deliberately
  collapses several peers and nothing available to this seam establishes which one
  the link currently points at. Sessions under the default `per-channel-peer` scope
  carry their peer in the key and are unaffected. Serving it needs a persisted
  `conversation -> principal` binding written at authorization time, which is a
  per-channel schema change. Every refusal is audited, so this is visible rather
  than silent.

  **Slack** never reaches here at all, because the ladder returns early for
  `SLACK_NAMESPACE`: its proactive traffic uses the gateway's own client.
  `test_channel_transport_outbound_authz.py` requires every shipped transport to
  override the method rather than inherit the permissive ABC default, so a new
  channel cannot skip the question.

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
- **Should it answer, above may it.** `telegram.forum_activation` (`always` |
  `mention` | `off`, anything else normalized to `always`) is the second decision,
  taken by `_activation_outcome` after `forum_gate_outcome` has authorized the turn:
  without it an allow-listed Topic cannot host a conversation between humans,
  because every message would start a turn. Scoped to non-private chats — a 1:1 DM
  is served in every mode, matching Slack's separate `slack_dm_activation`, since
  narrowing a noisy Topic must not silently mute the operator's own DM. A dropped
  message is SEL-audited (`denied_activation_off` / `denied_activation_mention_only`)
  and never reaches a turn.
- **`mention` reads Telegram's own entity classification, not the text.** Telegram
  marks a handle inside a link as a `url` / `text_link` entity and never as a
  `mention`, so `_mention_handles` (`telegram/client.py`) extracts the handles it
  called mentions and the gate tests membership. A text scan cannot tell the two
  apart: `https://host/@thebot/x` satisfies any `@handle` pattern, and
  `_flatten_text_links` appends a formatted link's TARGET into the text, so anyone
  able to post a link could otherwise hand the scan a handle to find and start a
  turn nobody asked for. Entity offsets are UTF-16 code units, which is why the span
  is sliced through a UTF-16 encode rather than by Python index — one emoji ahead of
  the mention shifts every later offset. A message carrying NO entity list at all
  (an album with no captions, a hand-built envelope) falls back to the token
  matcher, because "never parsed" is not "nobody was mentioned", and such a message
  has no auto-detected URL for the matcher to trip over either. A reply to one of
  the bot's own messages (matched on `bot_id`, not `is_bot`) also activates, since
  long-press-Reply is how a Telegram user addresses a bot without typing its handle.
  Both inputs are unresolved until `getMe` lands, and the gate answers False rather
  than True in that window so `mention` cannot behave as `always` at startup.
- **A host-wide LISTING also needs an UNAMBIGUOUS OWNER**, not just a DM. With more
  than one entry in `telegram.allowed_user_ids` the listing is refused and audited
  (`no_unambiguous_owner`), because an allow-list is a list of people permitted to
  talk to the agent and not a claim that one of them owns the install: under the
  default per-peer `dm_scope` those are separate sessions belonging to separate
  people, so listing every conversation hands one allow-listed human another's
  titles. This is the rule the owner notification already follows — "exactly one
  configured target, or nothing", which cites `/sessions`' owner-only rule as its own
  premise — reaching the surface it was named after. It costs an operator who lists
  two of their own accounts, the same cost accepted there, because the count is over
  ALL configured entries and a two-person allow-list is a guess either way. Scoped to
  the listings: a multi-person install still chats, stops, compacts and spawns.
- **A host-wide LISTING is refused outside a DM** —
  `TelegramDispatcher._require_direct_chat`, one gate for `/sessions` and `/cron`,
  refusing before the listing is built and auditing the refusal
  (`shared_topic_audience`). The allow-list gates who may DRIVE a turn, not who can
  READ the reply, and a Topic is readable by the whole supergroup: answering there
  would disclose every session on the host, or every scheduled job, to members who
  were never allow-listed at all. Slack's equivalents need no such rule (their reply
  lands in a DM or in a thread the caller is already in), which makes this the
  Telegram-specific half of one rule rather than a divergence from parity.
- **Scope follows the ARGUMENT, not the command name.** `/sessions` and `/cron` are
  listings outright; `/spawn` and `/task` are conversation-scoped with one argument
  and host-scoped with another, so they ask
  `messaging/commands.lists_host_state(command, arg)` — true for `list` and
  `status`, which
  render `manager.running` (every subagent on the box, with its task text) and the
  one global task runner, neither filtered on the session. That predicate lives
  beside the functions that BUILD those replies, because reading `/spawn <task>` and
  generalizing to `/spawn` is exactly how the listing first got through. It is not
  every host-touching argument: `task cancel` acts on the global runner but names
  nothing in its reply, and `/stop` and `/compact` are how a forum operator drives
  the Topic they are in, so refusing those would cost the forum surface with nothing
  disclosed in return. It takes the COMMAND as well as the argument because the
  normalization differs per command: `task_arg_reply` absorbs a leading `run`, so
  `/task run status` IS `status` by the time the listing is built, and both go
  through the one `normalize_task_arg` so the string dispatched on and the string
  classified cannot come apart.

## Mid-turn routing, queue receipts & cancel

A message that arrives while a turn is still generating is not a new turn: the
session semaphore is held, so running it directly would either block or open a
second conversation against the same key. Three channels carry the full
steer/queue/drain machinery — `telegram/transport_dispatch.py`,
`discord/transport_dispatch.py` and `teams/transport_dispatch.py`; all read the
same `messaging.queue_mode` (`config/loader.py`, `"steer"` | `"queue"`, anything
else normalized to `steer`) and all implement the same three primitives
(`_handle_busy`, `_enqueue_with_receipt` + `_drain_queue`, `_handle_stop`).

The **channel-neutral half of the queue receipt is shared**, not duplicated:
`messaging/queue_receipt.py` owns the receipt registry, the lock, the three
lifecycle transitions and the receipt body formatting. Each channel reaches it
through a `ReceiptSurface` whose address is bound at construction, which is why
the shared module never sees a `chat_id` / `channel_id` / forum thread and
Telegram's forum routing stays entirely channel-local. `_handle_busy` and
`_drain_queue` deliberately stay per-channel: they re-enter their own
`handle_message` (whose signature differs per channel) and own the per-channel
`_active_renderers`. `_handle_stop` is NOT in that exclusion — see
[Where a command handler splits](#where-a-command-handler-splits).

**Webex** also queues, on the shared pipeline rather than a fork. `drive_turn` is
awaited and releases the session semaphore in its `finally`, so by the time the
call returns the session is free and the dispatcher drains by re-entering its own
`handle_message`. Its receipt lives on its own message, which matters here
because Webex caps edits **per message**: a receipt on a separate message has its
own allowance and never competes with the answer placeholder's reserved final
edit. Adapting rather than forking is deliberate — leaving the shared pipeline
would mean re-deriving mute substitution, identity publication, the PreToolUse
gate, the auto-approve hook and four guarded post-turn steps for one feature.

**Teams is not in the steer-only group either**, and the distinction there is the
editable-receipt affordance rather than channel maturity: the Bot Framework
Connector supports `PUT {serviceUrl}/v3/conversations/{id}/activities/{activityId}`
for a bot's own activities, so `TeamsClient.update_message` can grow one receipt
bubble in place exactly as Telegram and Discord do. Teams therefore carries the
full machinery.

The remaining channels (WeCom, Weixin, Feishu) implement `_handle_busy` as
**steer-only**: they fold the message into the running turn and reply with a
one-shot notice, or ask the user to resend when steer is unavailable. They have
no receipt and no drain because their reply is bound to the inbound request, so a
hold-then-deliver follow-up turn could not be acknowledged and delivered reliably
later.

WeCom keeps that posture even though it CAN now push proactively: a held message
would still have to be answered against a request that was already answered, so a
`/queue` directive there is refused with a resend prompt rather than silently
steered — merging text the user asked to keep separate is the one outcome worse
than refusing.

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
**Discord's text commands are `!`-prefixed** (`!new`, `!compact`, `!model`,
`!status`, `!link`, `!unlink`, `!stop`, `!help`, `!sessions`, `!queue`,
`!steer`) because Discord's client swallows a bare `/`
message into its own slash-command UI; the `/` forms are also accepted as message
text for muscle-memory parity with Telegram, which uses `/` only. Since the
registered application commands landed (below) a `/` form may arrive by EITHER
route, so both resolve through the same `parse_command`.

The prefix is recognized only when the original text is not itself a command,
and the payload after it is **turn content, never a command**: `/queue /new`
queues the literal text `/new`.

A bare `/steer` or `/queue` carrying no message body matches neither the command
parser nor the override parser, so **both channels answer it with the
directive's usage**: the alternative is handing the literal string to the model,
which then answers it as chat text, indistinguishable to the user from the
feature not existing. `is_bare_mid_turn_override` is the predicate, one per
channel (`telegram/commands.py`, and `discord/commands.py`, which matches the
token as typed because Discord has no `@BotUsername` suffix to strip), and each
dispatcher consults it under the same two gates: `interpret_as_command`, so a
caption on an attachment is never read as a bare directive and its file silently
dropped, and `override_mode is None`, because a directive that HAD a body has
already been stripped off the text. Each reply names that channel's own prefix
(`!queue <msg>` on Discord, `/queue <msg>` on Telegram).

Discord adds a second guard at the same site: an unrecognized `!token` that
Discord's own name grammar would accept gets the usage card
(`unknown_command_usage`), so a mistyped `!sesions` reads as a typo rather than
reaching the model. It is a shape test and not a list of near-misses (at least
two characters, leading letter, so `!!!`, `!?` and `!5` fall through), `!`-only
because a `/`-leading message the client did send is more likely a path than a
command, and it defers to `parse_command` and the directive alias sets so a real
command can never be answered with the card.

### Hard cancel: `/stop`

`/stop` (alias `/cancel`; `!stop` / `!cancel` on Discord) aborts the running
turn, drops every queued message, and finalizes the receipt to `🛑 Cancelled`.
`clear_queue` and the receipt finalize run together under `ReceiptQueue.lock`.
All of that, including both reply strings, is
`messaging/commands.py::stop_running_turn(sessions, session_key, *, queue,
surface)`; a dispatcher supplies the session key and its bound `ReceiptSurface`
and sends the returned text.

**Cancel is cooperative before it is fatal.** The shared handler calls
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

### Where a command handler splits

A dispatcher's command handler is two things welded together: a **decision**
(what the grant becomes, whether a turn was actually cancelled, how long a login
link lives, which bindings a rebind displaces) and a **send**, which needs a
`chat_id`, a `channel_id`, or a `(conversation_id, serviceUrl)` pair. The
decision half is identical across channels and is shared; only the send stays
behind. Every shared handler therefore **returns the reply text rather than
sending it** — the shape `release_conversation_location` already used — so a
user-facing string has exactly one owner.

| Command | Shared half | Per-channel half |
|---|---|---|
| `/stop` | `commands.stop_running_turn(sessions, session_key, *, queue, surface) -> str` | the send; which session key a resumed conversation stops |
| `/yolo` | `commands.run_yolo_command(arg, *, source, caller, phrasing) -> str` | the send; `source` (also the grant's audit source), the trusted `caller`, and a `YoloPhrasing` |
| `/link` | `link.rebind_conversation_location(sessions, *, key, location, unlink_command) -> str` | the send; `location` (the channel's one spelling of "this conversation"); any refusal only a resume-capable channel can hit |
| `/unlink` | `link.release_conversation_location(sessions, *, key, location, channel) -> (str, swept)` | the send; the opt-out write ordered before it; any dashboard nudge for a swept binding |
| dashboard link | `commands.parse_dashboard_ttl(arg, *, parse_duration) -> int`, `commands.format_ttl` | the command GRAMMAR — which word the TTL is (Telegram's `parse_dashboard_argument` reads the third; Teams' `/dashboard <ttl>` the second) |

Four constraints shape those signatures:

- **No address reaches `messaging/commands.py`.** It takes no `chat_id`,
  `channel_id`, `conversation_id` or thread, and reaches a receipt bubble only
  through the already-bound `ReceiptSurface`. That is what keeps Telegram's forum
  routing and Teams' service URLs channel-local, and a parameter named for one
  channel's address is how the module would acquire its first per-channel branch.
- **Command spellings are data, not prose.** A channel that renders inline code
  writes `` `/yolo on` `` where Telegram writes `/yolo on`; the two spellings are
  `YOLO_PHRASING_PLAIN` / `YOLO_PHRASING_MARKDOWN` and the `unlink_command`
  argument, so a channel picks a value instead of restating the sentence —
  restating it is how three copies drifted.
- **No word count crosses a channel boundary.** `parse_dashboard_ttl` takes the
  already-extracted ARGUMENT, never the message, because indexing into the split
  text would read one channel's grammar on another's behalf.
- **`parse_duration` is injected, not imported.** It lives in
  `dashboard/token_auth.py`, and `kiro_crew.messaging` imports nothing from
  `kiro_crew.dashboard` at any nesting depth (`test_messaging_commands.py`
  scans for it, deferred in-function imports included).

Two things deliberately stay duplicated. `_handle_busy` and `_drain_queue`
re-enter their own `handle_message`, whose signature differs per channel, and own
the per-channel `_active_renderers` (see
[queue receipts](#mid-turn-routing-queue-receipts--cancel)). And `/yolo` has no
Discord counterpart at all: Discord renders real Approve/Deny buttons, so an
out-of-band grant is not what makes tools usable there the way it is on Teams.

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
before the split. Discord gets that from the shared `split_markdown_safe`, whose
final chunk is deliberately left open as the live buffer; Telegram still carries
its own splitter. Raw markers never reach posted text; each renderer keeps a
defensive raw-marker parser only for callers that bypass `TurnDriver`.

## Session privacy modes (`privacy_mode.py`)

`!temporary` (blank-slate: no memory reads, no writes, no persistence) and
`!incognito` (reads allowed, writes blocked, ephemeral log discarded on close)
were Slack-only machinery in `slack/handler.py`. The channel-neutral core lives
here so a second channel inherits the trackers, the durable flag and the audit
rather than a second copy of them; `slack/handler.py` keeps every public symbol
(`is_thread_temporary`, `is_thread_incognito`, `_is_slack_restricted`,
`maybe_apply_privacy_modifiers`) as a thin wrapper, with `_thread_temporary` /
`_thread_incognito` as **aliases of the shared objects**, not copies.

- **Keyed by session key, never by a platform thread id.** That is what lets one
  copy serve a Slack thread ts, a Telegram DM route and a forum Topic, and it is
  why `is_restricted(session_key)` answers correctly for a
  `telegram:{agent}:direct:{user}` key. `PRIVACY_LRU_MAX` (10,000) bounds each
  tracker; eviction is least-recently-marked.
- **`hydrate(sessions, session_key)`** rebuilds the process-local trackers from
  the durable `SessionMap` flag (the mode name IS the flag name), so a mode
  survives a gateway restart. `conv_state_map` requires the real `SessionMap`
  class rather than any attribute: a `MagicMock` stand-in returns a **truthy mock**
  for every flag, which would mark every session both temporary and incognito —
  failing closed, but wrongly and silently.
- **`apply_mode(mode, session_key, *, source, caller, resources, sessions, notify,
  on_applied) -> bool`** is idempotent and returns whether the mode was NEWLY
  applied. The in-memory mark lands FIRST, before any await, so a concurrent
  inbound message cannot observe the session as unrestricted after the user asked
  for privacy; then the durable write, the audit (`f"{source}.{mode}_mode"`), the
  caller's `on_applied` hook, and the notice. A persist failure is logged, not
  raised — the mark already holds for this process, and refusing the modifier would
  tell the user privacy is off while it is on.
- **`strip_and_apply(text, session_key, *, source, …) -> (text, only_modifier)`**
  is the single-text entry point. `only_modifier` means the message was nothing
  but modifiers and the caller MUST return without starting a turn. Slack drives
  the primitives (`strip_token` + `apply_mode`) directly instead, because it
  carries two texts and only the mention-stripped command text decides
  `only_modifier`.
- **Everything platform-shaped is a parameter**: `source` (the audit label),
  `sessions` (only to reach the one `SessionMap`), `notify` (delivers
  `NOTICE_TEMPORARY` / `NOTICE_INCOGNITO`, held here so two channels cannot
  describe the same mode differently), and `on_applied` (Slack's `set_slack_link`,
  so follow-ups pass its in-active-thread gate).
- **`strictest(modes) -> str`** collapses several requests into the one mode a
  shared turn can carry, for a channel whose queue drain answers a burst of
  messages as a single turn under a single key. Ranked on `_STRICTNESS`, which is
  declared SEPARATELY from `_MODES` — that order is about which token to strip
  first, and a third mode could need one position for parsing and another for
  strength — with a test pinning that every mode appears in both, since a mode with
  no rank would sort last and a privacy mode failing toward permissive is the wrong
  direction. Temporary outranks incognito because it forbids a superset. An
  unrecognized name is ignored rather than raised: this runs on the delivery path,
  where refusing a turn over a mode that does not exist would cost the user their
  message.

**A modifier typed MID-TURN has to travel with its message.** On the channels that
carry the queue, the command ladder strips the modifier off the text and defers the
apply until after rotation, so the mark lands on the key the turn really runs under.
The busy path returns before that point, and the drain re-enters `handle_message`
with `interpret_commands=False` on text the modifier is already gone from — so
nothing downstream can re-derive it. Telegram therefore carries the request as queue
state (`enqueue(..., privacy_request=)`, alongside `attachments`) and applies it to
the key the drained turn resolves, including on the re-enqueue that defers a burst
past `MAX_COLLAPSE`. The two mid-turn branches get different treatment because they
run under different keys: a STEERED message folds into the turn already running on
the current key, so its mark lands there immediately (and only once the steer is
known to have landed, so a refused steer that falls through to the queue does not
leave the session restricted); a QUEUED message is marked later, where its own key
is known.

**Every durable write is covered, not just the transcript.** `set_title` reaches
`ConversationLog.update_metadata`, which CREATES the session file — so a
`/title` on a restricted conversation persisted user-authored content for a mode
that had just promised not to. Telegram's `_handle_title` now gates on
`is_restricted` and says so in its reply, matching `_persist_turn` and Slack's own
`/title`. The predicate is the channel's in-process tracker and NOT the transcript's
`memory_mode` header, because the dashboard deliberately writes an incognito
transcript and marks it, discarding on close: a gate down in `ConversationLog`
would refuse a write that path is entitled to make. `test_telegram_parity.py`
enumerates the title writes in every dispatcher that reaches `privacy_mode` and
requires each to consult the predicate — scoped that way because the eight channels
that do not offer the modes have no session that can BE restricted.

**Enforcing temporary takes TWO gates, and they are not the same predicate.** The
write half is `is_restricted` (`_is_slack_restricted` on Slack), consulted at every
transcript, memory and artifact write. The read half is one kwarg —
`ContextBuilder.build_message(..., blocks_reads=)`, which suppresses the memory and
lesson blocks — and it defaults False, so a channel that omits it injects yesterday's
memories into the prompt of a thread `NOTICE_TEMPORARY` told the user reads nothing.
Every dispatch path passes it: Slack's native `handle_message`, Slack's default
`transport_dispatch`, and Telegram's. The predicate there is **`is_temporary`, never
`is_restricted`** — incognito is documented as reading memory and refusing only to
write, so the combined predicate would take memory away from a mode meant to keep
it, and it would do so silently.

The dashboard's `_is_restricted_session` / `_blocks_reads_session`
(`dashboard/handlers/_shared.py`) reach this through `is_channel_session_key(sk)`,
not `sk.startswith("slack:")`. The narrow test made that branch structurally
unreachable for every other channel, so a Telegram session the user marked
incognito could never enter it and the ~30 dashboard mutations gated on the
predicate stayed open for it.

## Auto-titling (`auto_title.py`)

After the first successful turn a channel conversation is named only if the user
typed a name; otherwise every surface shows a deterministic fallback. One short
background turn asks the model for a name instead. This was Slack-only AND dead on
Slack's own default path: `_maybe_auto_title_slack` was called from the native
loop and nowhere else, while `messaging.use_transport` defaults `True` — so on a
default install no Slack session was ever LLM-titled.

- **`maybe_auto_title(sessions, conv_log, session_key, user_text, assistant_text,
  *, source, resources="", set_channel_title=None) -> str`** returns the applied
  title, or `""` when nothing was applied. `set_channel_title` is the optional
  caller-side callback that renames the conversation on the platform (Slack's
  `set_thread_title`), which is what keeps `messaging` free of any channel import;
  a channel with no renameable conversation omits it and still gets the transcript
  title. `conv_log` may be `None`.
- **The claim is shared, and it is check-and-mark in ONE synchronous step.**
  `try_claim(session_key)` is called by the caller *before* it fires the task, so
  two turns racing — including two turns on two different channels that resolved
  to the same session key — produce exactly one naming turn. A SKIP verdict or a
  transient failure calls `release_claim` so the next exchange retries; a message
  arriving inside that window is intentionally skipped rather than double-titling.
  `TITLE_LRU_MAX` (10,000) bounds the tracker.
- **A person's name always wins, and it takes TWO guards** because they cover
  different windows. The in-process one (`TITLE_KIND_MANUAL` recorded on the claim)
  catches a rename that lands while the naming turn streams. The persisted one is
  `ConversationLog.update_metadata_if` with a "the record still carries no title"
  guard, evaluated under the cross-process lock: after a restart the claim tracker
  is empty and the claim is taken again, so without it a manual rename made in an
  earlier process is silently replaced. When that guard refuses, the channel title
  is left alone too — otherwise the two surfaces would disagree about what the
  conversation is.
- **The turn is tool-free and bounded.** Every `EVENT_PERMISSION_REQUEST` is
  rejected and audited (`auto_title.tool_rejected`): the prompt is built from text
  the model itself produced, so a tool call there is prompt-injection reach, and an
  unanswered request wedges the agent process. One turn,
  `TITLE_TURN_TIMEOUT_SECS` (30s), `TITLE_INPUT_CHARS` (200) per side, capped at
  `TITLE_MAX_CHARS` (80) after credential/exfiltration redaction. `get_lock()`
  serializes the shared background session and rebinds per event loop.
- **No model id, anywhere.** The turn runs through `llm_helpers.background_turn`,
  so the model is whatever the shared background session was created with
  (`agent.role_models.background`, default `"auto"`). Spend is labelled
  `bg:{source}_auto_title` so it is attributable per channel.
- `clean_title` keeps the first line, trims quoting, and drops `<`/`>` — they open
  a link in Slack's mrkdwn and a tag in Telegram's HTML, and a title is rendered
  as-is on both.

## Slack reference implementation

### `SlackTransport` (`slack/transport.py`)

Wraps `SlackClientOps` in the Layer-1 contract; declares Slack's real (rich-end) capabilities: `streaming/edit/reactions/files/rich_blocks/threads=True`, `max_message_chars=3900` (`SLACK_MSG_LIMIT`, the shipped send path's split point), `max_buttons=10` (the checkboxes-element options cap). `authorize()` is **deny-by-default & owner-only** — an empty `allowed_users` frozenset (copied at construction so it can't mutate mid-decision) authorizes nobody, and every denial (including empty/missing `user_id`) is SEL-audited (`operation="slack_transport.authorize"`, `outcome="denied"`). `receive()` acks → runs the trusted-bot gate → normalizes to `InboundMessage` → authorizes → invokes the injected `dispatch` callback. Bot-authored events are admitted ONLY on a positive `bot_id` match against the `trusted_bot_ids` allow-list (a constructor param mirroring `allowed_users`, frozen at construction, empty by default so every bot event drops); the gate mirrors the Socket Mode drop site in `slack/events.py` — the gateway's own bot id is never trusted even when listed (`error=own_bot_id_never_trusted`), an unverified self identity fails closed (`error=trusted_bot_requires_verified_self_id`), untrusted-bot denials are SEL-audited (`operation="slack_transport.receive"`, `error=untrusted_bot`) with the trust decision running before the `subtype == "bot_message"` filter so a trusted bot's `bot_message` is not eaten, and an admitted bot's `bot_id` stands in as `user_id` with the admission audited (`outcome="allowed"`, `resources="trusted_bot"`). Loop bounding (the per-thread trusted-bot turn cap) stays the dispatch layer's job. The client is held **and exposed** via a `client` property (guardrail G2).

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

Full new-path dispatch: fires the ack reaction + working status immediately (constructing the `SlackRenderer` before the potentially slow session acquisition), acquires/creates the session, builds the message with context, then drives `TurnDriver.run()`. Agent resolution: thread override (`!agent`) → per-channel `agent_override` → configured default → the canonical `_DEFAULT_KIROCREW_AGENT = "kirocrew"` fallback (so the session loads kirocrew-core / `spawn_run` rather than kiro-cli's bare built-in default). It injects `auto_approve_tool=lambda title: _should_auto_approve_spawn(context_builder, title)` and `auto_approve_session=lambda: is_slack_session_trusted(session_key)`. Post-turn bookkeeping (context-usage accounting, conversation logging, the fire-and-forget [auto-title](#auto-titling-auto_titlepy), success SEL audit) is each isolated in its own `try/except` so a bookkeeping failure never re-records a successful turn as a failure; `sessions.release()` runs in `finally`. The auto-title requires a non-empty reply and an unrestricted session, and claims through the shared tracker so the native path cannot title the same conversation twice.

**Partial-progress rescue on the failure path.** A turn killed mid-flight (transient backend fault, dropped stream) used to discard everything the model had already produced, so a retry re-read a transcript ending at the question and started over — observed as five consecutive attempts on one thread with the session file still 625 bytes at the end. The `except` branch now persists what the user was already shown, suffixed with `PARTIAL_TURN_MARKER` (defined in `slack/renderer.py`) so the next turn can tell a reply that stopped mid-sentence from a finished one and does not treat the work as already reported.

What it persists is `SlackRenderer.delivered_text`, a **delivery ledger** — not the model's output. The distinction is the whole point: `_accumulated` grows the moment a chunk arrives, but a chunk only reaches Slack when the `_EDIT_INTERVAL` throttle opens, so persisting that would record text nobody saw as established fact and the retry would build on something that was never on screen. Only `_append_stream` advances the ledger, and only when Slack acknowledged the append. Two cases deliberately do **not** advance it:

- **The `chat.update` cursor fallback** (no streaming surface). `_safe_update` returns `None`, swallows its own exceptions and truncates at `SLACK_MSG_LIMIT`, and the frame it sends is a fence-safe prefix rather than the whole text — so delivery is unconfirmable there. The ledger stays empty and the rescue no-ops, which is the correct outcome.
- **A rejected append**, including after the one stream rotation retry. A send Slack refused is not delivery.

The ledger holds text as SHOWN — thinking tags stripped, `[OPTIONS:…]` markup suppressed by the bracket-hold, image-adjacent text still withheld by `_ref_hold` until the seal releases it. It is always a subset of `_accumulated`, never a superset. Appends are cumulative and final on this path (`chat.stopStream` does not replace them), so the ledger needs no reconciliation when `_accumulated` is reset at a `wait` boundary.

Guards, so the rescue can never make a failure worse: skipped when the reply already landed (`_stamped_turn`); skipped when the turn already ended normally (`SlackRenderer.turn_finalized`), because a fault raised after a finished stream unwinds through the same `except` and marking that complete reply as cut off would tell the next turn to resume finished work; the rescued row carries `source_user` so it keeps the same Slack-user provenance as every success-path write; skipped for Slack-restricted (incognito/temporary) sessions, matching the existing write points; `strip()` decides emptiness only, and the ledger text is persisted verbatim because leading indentation is content in a transcript; run off-loop via `asyncio.to_thread` because `ConversationLog.append` takes a cross-process flock; wrapped in its own `try/except` so a disk fault cannot replace the real exception in the log.

The rescue also runs **only when the user row is confirmed on disk** (`_logged_user_turn`). When the receipt append raised, whether the row landed is unknowable from the dispatcher: `ConversationLog.append` writes the row and only then invalidates caches and calls `_maybe_rotate`, whose own guard covers just the `stat` — so a rotation fault on an oversized transcript raises with the row already durable, while a lock timeout raises with nothing written. Writing both rows there would duplicate the question; writing the assistant row alone would orphan the answer. So the rescue no-ops and that retry starts from the question, exactly as it did before this change — the same rule the ledger applies to an unacknowledged send: when an outcome is unconfirmable, record nothing.

This is why the rescue never writes the user row itself. The receipt append always precedes streaming, so any turn with text worth rescuing has already attempted it, leaving only "confirmed written" and "unknowable" — there is no reachable state in which the rescue both has text and must recover the question.

**This is the only rescue in the codebase, and that is deliberate — because Slack is the only renderer that can say WHICH bytes the user retained, not because it is the only one that emits mid-turn.** Several renderers the shared `drive_turn` pipeline serves do send during a turn: `telegram/renderer.py` live-edits the current segment per chunk (`_stream_live`), `wecom/renderer.py` pushes stream frames from `on_text_chunk` (`_push`), and `webex/renderer.py` rides the buffer tail on a status frame. None of them yields a per-append record of acknowledged output — those frames are throttled, replaced wholesale, or truncated — so on those channels there is no honest ledger to rescue from, and persisting produced-but-undelivered text would record an undelivered reply as a completed turn, which `weixin/turn_renderer._send` documents and `test_weixin_dispatch.py::test_delivery_failure_is_not_recorded_as_success` guards. `capabilities.streaming`/`edit` cannot be used to tell the cases apart either, because `transport.py` marks both aspirational and explicitly unenforced.

### Two Slack features are deliberately NOT the reference

Slack is the reference for the LAYERS — transport contract, renderer, turn
dispatch — not for its feature list. Two of its features are decisions against
replication rather than parity gaps, so an audit that finds another channel
lacking them should close the finding, not file it:

- **A channel-local auto-approve switch** (`is_slack_session_trusted`, the
  `mc_tool_trust_` button). Approval posture is a property of the agent, not of
  the surface a message arrived on: a second place to turn approvals off is a
  second place to forget one is off, and it makes a session's ceiling depend on
  which app the user typed into. Every other channel routes `/yolo` through the
  one `safety_override` grant — see "There is ONE auto-approve grant" under
  [Invariants](#invariants).
- **A channel-local reply redirect.** Reply routing is owned by the session's
  origin binding that every channel already shares through `drive_turn`. A
  second router for the same question disagrees with the first the moment either
  changes, and the disagreement surfaces as a reply delivered to the wrong
  conversation.

## Cron output delivery: one run, one surface

An unattended cron run has no inbound message to answer, so its output is
delivered proactively, and `slack/gateway.py` chooses ONE surface instead of
broadcasting to every one it can reach. The choice is that a job belongs to the
conversation that scheduled it: `job.session_key` records the creating session's
key, and `_cron_origin_key(parent_key)` recovers it from the run's
`cron:{job_id}` / `cron:{job_id}:{run_id}` key, which carries no channel namespace
of its own and so can never name the surface itself.

- **An explicit `job.channel` still wins, on all three legs.** A pin means the
  user named where they want to be told, so the channel leg is SKIPPED for a
  pinned job and Slack delivers to the pin. This has to hold on the result leg,
  the run-failure alert and the crash alert alike: because the Slack leg stands
  down on a confirmed channel delivery, a channel leg that ignores the pin does
  not merely add a surface, it REPLACES the pinned one — the alert lands on the
  origin conversation and never where the user asked.
- **Otherwise a DELIVERED channel send takes it and the Slack owner-DM leg stands
  down.** Every leg gates on the boolean `_deliver_cron_to_channel` actually
  returned, never on a prediction that the channel *would* take it: governance can
  refuse the send and the wire can fail, and standing Slack down on a prediction
  loses the run's output outright in exactly the cases the fallback exists for.
  That send redacts at the canonical egress (credentials and exfiltration URLs
  both) and chunks to the transport's own ceiling. Delivering to both would notify
  one operator twice for one run, which is how notifications become noise people
  stop reading.
- **A markup cut is an egress, and appended text is final.** Slack streams by
  appending, so two appends render as one run of characters. The image-withhold
  path therefore holds a `_REF_HOLD_LOOKBEHIND_CHARS` margin BEFORE the span as
  well as the span itself, and `_release_refs` redacts the string it returns.
  Both halves are required: cutting exactly at the span start puts a straddling
  credential's two halves in separate appends, each individually clean while the
  rendered concatenation spells the key, and the driver's rolling redactor cannot
  see it either because the hold reordered the text out of its window. Redacting
  inside `_release_refs` rather than at the append site keeps the guarantee with
  the join that creates the hazard.
- **The display-form floor lives at the SINK, in both renderers.** Slack's
  `_display_safe` is the twin of Discord's `_redact_transformed`, and every
  model-authored egress in the file passes it: the sealed body, an appended stream
  chunk (final on that path, so an unscanned append is unrecoverable), the posted
  thinking block, an upload-rejection note (whose destination came from the model,
  inside italics Slack renders away), and the released image-markup tail. The gap
  was found three times on three different lines before it was moved to the sink,
  which is the argument for the sink: a per-line scan checks one of the two forms
  that leave, and the reviewer has to find each line. It is idempotent, so a path
  that meets it twice costs nothing.
- **Every sink reports to `delivery_failed`.** The property answers "the user saw
  NOTHING", so it counts an attempt and a landing at each place output leaves the
  renderer: the segment seal, the markup recovery after a failed upload (LANDED
  only -- the seal that fell through already counted the attempt, and counting a
  second would make a recovered turn look like two failures), and the placeholder
  for an empty-bodied turn (which, with no earlier seal, is the whole delivery). A
  sink that delivers without reporting turns a success into a duplicate cron
  alert; one that fails without reporting files a silent turn as a success.
- **Every proactive egress meets the display-form floor, not just the literal
  one.** A renderer is where a TURN gets that floor, and no proactive path passes
  one, so the floor sits at each of the three chokepoints instead:
  `api_send_message` (before any leg reads the body), `_deliver_channel_reply`
  (cron results, run-failure and crash alerts, subagent completions), and
  `_deliver_channel_dm` (so a future caller cannot reach a channel without it).
  A literal-only scan there let a markdown-collapse credential (`AKIA**...**`,
  which the client renders whole) reach the channel, and every caller inherited
  the gap rather than each one carrying it. Applying it twice on one path is
  deliberate: the redactors are idempotent and the guarantee belongs at the
  egress, not at whichever caller exists today.
- **The SEL trail names the surfaces the alert LEFT ON.** `downstream_service`
  lists `slack` only when the Slack post itself landed, and the channel namespace
  only when the channel send returned True. A configured client is not a delivery:
  reading `self.slack` there recorded a Slack egress for every Discord-only alert,
  which corrupts the one question the record answers — where did this content go —
  in the direction that overstates exposure.
- **Slack remains the delivery for a Slack-origin, dashboard-origin or
  origin-less job**, which is every job an install carries today, so nothing that
  works now changes: `_deliver_cron_to_channel` returns False for those and the
  Slack leg runs as before, opening the owner's DM when the job stores no channel.
  The dashboard notification is resolved ahead of this choice and is unaffected by
  it.

Three legs carry the rule: the run's result, the post-subagent response
(`_deliver_cron_response`, which tries the channel FIRST so a Slack-less install
still delivers, and skips Slack once it has), and the run-failure alert (gated on
the channel leg's confirmed delivery). The crash-alert path, where the run raised
rather than returning a failure, is the exception and still sends both legs,
gated only on `job.silent`. Dedup state (`last_posted_hash`,
`last_failure_hash`) advances once EITHER leg delivered, because a Slack-less
install would otherwise never advance it and would re-deliver an unchanged result
on every run.

**Both ends of a cron send are governed, tightest-wins.**
`_deliver_channel_reply` vets the DESTINATION conversation (`origin_key`), so
`_deliver_cron_to_channel` vets the ACTOR as well: `actor_key` is the
`cron:{job_id}` surface, and cron is the unattended surface an operator restricts
hardest, so evaluating only the destination would let a cron-surface `channels`
denial stop applying the moment cron routed through a channel it does not itself
own. Both go through the same audited, fail-closed `channels` ladder, off-loop
because resolving the active profile walks the profile directory. An unusable
answer is not permission: a raised evaluation and a `Decision` without
`permitted` both refuse the send.

## Invariants

- **One-way dependency**: `kiro_crew.messaging` never imports `kiro_crew.slack` / `kiro_crew.dashboard`; violations reintroduce the cycle the abstraction removed. This holds at **any nesting depth** — a deferred in-function import is still an edge, so a shared helper that needs something from a surface takes it as a parameter (`parse_dashboard_ttl`'s `parse_duration`). `test_messaging_commands.py::TestLayering` scans the package's ASTs for it. There is exactly ONE recorded exception, and it is recorded as a `(file, module)` pair with a reason rather than as a hole in the scan: `dispatch.py`'s `build_directive_consumer` reaches `dashboard.session_directive_apply`, the SHARED applier the dashboard's own consumer uses, so the dashboard-only denial and the monitor-trio authorization chokepoint live in one place. Injecting that applier as a parameter — the pattern the TTL helper uses — would put a security boundary behind a caller-supplied callable, which is the worse trade. A companion test deletes the entry the moment the edge goes away, so the list cannot rot into a standing pre-authorization.
- **A hoisted command carries its AUDIT with it.** `cron_command_reply` emits the
  `cron.remove` and `cron.batch_delete` SEL events that used to live in Slack's own
  copy of the command, so hoisting neither dropped Slack's trail nor left the other
  channels deleting cron jobs without one. `source` and `caller` are keyword-only
  with empty defaults: a channel that has no user id in hand still leaves a record
  with the surface as the subject, because once the jobs are gone from `crons.json`
  the trail is the only way to tell a deliberate remove-all from data loss. A
  contended store is audited as nothing, since the delete never happened.
- **A shared command handler returns reply TEXT, never sends, and takes no address**: the send is the only per-channel half, so `stop_running_turn`, `run_yolo_command`, `rebind_conversation_location` and `release_conversation_location` all hand back a string. Nothing shared accepts a `chat_id` / `channel_id` / `conversation_id` / thread; a receipt bubble is reached only through the already-bound `ReceiptSurface`. Accepting one address would put the first per-channel branch inside the shared module and put Telegram's forum routing and Teams' service URLs back in scope for it.
- **Deny-by-default authorization**: `MessagingTransport.authorize` implementations authorize nobody when unconfigured; interactive approval denies unless positively approved (or a timeout elapses → deny).
- **Redaction is unconditional**: all LLM/tool-originated text flowing through `TurnDriver` passes `redact_exfiltration_urls()` + `redact_credentials()` before reaching any renderer.
- **Protocol metadata is not assistant speech**: streamed steering frames are withheld until complete, removed even when split across chunks, and represented as a structured boundary. Summary-bearing compaction activity is never sent to a channel as assistant speech; only a terse receipt may be rendered. `[OPTIONS: …]` remains user-facing and is never stripped by the shared filter.
- **Conservative capability defaults**: unspecified `TransportCapabilities` degrade safely (WhatsApp-like floor), and renderers must honor `max_message_chars` (`chunk_text`) and `max_buttons`.
- **Table rendering is per-target and outbound-only**: `messaging/tables.py` runs on the bytes a renderer is about to send, never on the canonical text `TurnDriver.run` returns or on the `text()` accessors that feed history — so the dashboard keeps the authored pipes while a pipes-only channel gets cards. A target may only set `table_mode=native` when its `TransportCapabilities.native_tables` is true; setting it without the capability resolves to `cards`, because raw pipes are the one output the conversion exists to prevent.
- **A channel's keyword commands are one copy of their reply text**: `spawn`,
  `cron`, `task run` and the stats line live in `messaging/commands.py`, and Slack
  delegates to them rather than holding the originals. A channel adds its own
  grammar (`/cron` vs the bare `cron` keyword) and nothing else, so the two
  channels cannot answer the same question differently. Where a command starts
  something that later needs to reach the operator back, the conversation travels
  with it: both `task_command_reply` (the bare `task run <spec>` keyword) and
  `task_arg_reply` (the already-parsed `/task run <spec>` form) take a
  keyword-only `session_key=""` and forward it to `runner.start_background`, so
  the same run escalates its approval notices to the same place whichever grammar
  typed it. A caller that omits it keeps the narrow `(path, source=)` call, which
  is what a duck-typed stand-in still accepts.
- **A task spec path is validated before it is read, on the channel path too**: a
  spec's CONTENTS reach the model, so an arbitrary path is an exfiltration
  primitive rather than a usability question (`task run ~/.ssh/id_rsa`). Both
  grammars route through `hooks.validate_file_path`, which applies the Windows UNC
  trusted-root check before resolving (a `realpath` on a UNC path is itself the
  outbound SMB probe) to the raw and anchored forms, refuses a path with a
  linked ancestor on Windows (the walk or the resolve would itself be the
  probe), screens a Windows leaf link's own target (readlink, a local
  metadata read) so a link aimed at an untrusted UNC share is refused while
  benign leaf symlinks still resolve, canonicalizes through every symlink on
  POSIX, and refuses a resolved
  target under a sensitive root, so an innocent-looking path that resolves into a
  blocked root is refused through the link. The **canonical** path is what reaches
  `runner.start_background`, not the raw argument, because validating one string and
  acting on another is how a guard becomes ornamental. The refusal names neither the
  path nor the reason, since distinguishing "sensitive" from "missing" is an oracle
  for probing which roots exist on the host. This matches what the dashboard's
  `/api/taskrunner/start` already did; the channel keyword was the surface missing
  it.
- **A session-scoped gate is keyed by SESSION KEY, and its predicate never tests a
  namespace**: `privacy_mode.is_restricted` is a dict lookup, so a Slack, Telegram
  or Discord key all answer the same way. A caller narrowing to
  `startswith("slack:")` does not fail loudly — it makes the branch structurally
  unreachable for every other channel, which is how the dashboard's restricted-session
  mutations stayed open for a non-Slack incognito session. Use
  `link.is_channel_session_key` when the question really is "is this a channel
  session at all".
- **A conversation is titled at most once, through ONE claim tracker**:
  `auto_title.try_claim` is check-and-mark in a single synchronous step, and the
  tracker lives in `messaging/auto_title.py` rather than per channel, so two turns
  that resolved to the same session key cannot both spend a naming turn on it. A
  generated title never replaces a name a person chose: the in-process
  `TITLE_KIND_MANUAL` claim covers a rename mid-stream, and
  `update_metadata_if`'s "no title on the record" guard — re-decided under the
  cross-process lock — covers a rename made before a restart, when the tracker is
  empty and the claim is taken again.
- **Redaction is applied against the form a channel RENDERS, not only the bytes it
  sends**: any renderer that converts markdown to a markup the platform then hides
  must run `display_safety.redact_for_display` at its own seal, because the
  byte-level pass in `TurnDriver` ran before those tags existed. Slack, Discord and
  Telegram each do this at their render boundary.
- **A media-only inbound message is a message**: a transport whose text extraction comes back empty may only drop the envelope when there are also no media items. Weixin previously returned early on empty text, so an uncaptioned screenshot was discarded with no reply and no log line — the sender saw a successful send while the agent was never told anything arrived. Emptiness is a reason to drop only when the whole envelope is empty.
- **Weixin inbound media is CDN-indirect**: iLink envelopes never carry bytes, only a `CDNMedia` reference (`encrypt_query_param` + `aes_key`) whose object is AES-128-ECB encrypted on the WeChat CDN. `weixin/media.py` owns that protocol work (URL construction with percent-encoded params, key decoding, decrypt, a streaming size cap enforced on bytes read rather than `Content-Length`); `weixin/attachments.py` maps the four CDN-backed item types onto the shared `Attachment` and hands them to `messaging/attachments.py`, which keeps classification, limits, signature validation and temp-file ownership channel-neutral. The `aes_key` field carries **two** encodings for the same value — `base64(raw 16 bytes)` for images, `base64(ascii hex)` for file/voice/video — discriminated by decoded length plus a strict hex check, because guessing wrong yields plausible garbage rather than an error. A voice item that already carries server-side `text` short-circuits the download: iLink voice is SILK, which no shipped transcription backend decodes, so the local path is strictly worse than the transcript the server gave us. `files_inbound=True` reflects this; `files_outbound` stays `False` until the `getuploadurl` + encrypted CDN PUT half lands.
- **A mid-turn queue receipt is edited, never deleted**: it flips in place to `▶️ Now answering` on drain and to `🛑 Cancelled` on `/stop`. It is the durable record that a held message was accepted, so no path may delete it.
- **A queued burst drains as ONE turn**: `_drain_queue` joins the held texts in arrival order into a single combined turn (capped by `_MAX_COLLAPSE` and, on Discord, the attachment ingest limit), never N replayed turns. Anything past a cap is re-enqueued together with everything behind it so FIFO order stays exact.
- **A mid-turn steer requires a genuinely live turn**: gate on `provider.has_active_turn()`, never on `sessions.is_busy()` alone, which stays true through post-turn bookkeeping. Steering an ended prompt is silently swallowed, producing an acknowledgement with no answer.
- **Cancel is cooperative before it is fatal**: `/stop` sends the ACP `session/cancel` notification and lets the turn stop at its next safe point; escalation to a hard kill happens only after the soft-stop budget elapses without an ack. On a shared runtime the cooperative path is the only one that cannot take a co-tenant down with it.
- **Transport shutdown is quiescent**: a client that fast-acks inbound work in background tasks cancels and awaits those tasks before closing their shared network session or returning from shutdown. Teams owns this ordering in `TeamsClient.close()`, so a gateway teardown cannot leave a turn unwinding against an already-closed Connector session; `WeComClient.close()` owns the same one for its turn tasks, which borrow the client's `aiohttp` session for the `response_url` fallback.
- **An at-least-once inbound callback is deduped before it drives a turn**: a protocol that documents redelivery (WeCom's `msgid`) needs a bounded suppression window, because a repeat costs a second provider round-trip and repeats every tool side effect. The window is consulted AFTER authorization so unauthorized traffic cannot evict genuine entries, and a frame with no id is never suppressed — no id is no evidence of a duplicate, and dropping it would lose a real message.
- **A dropped outbound send is loud, never a return value**: `TeamsClient._post_activity` raises `TeamsSendError`. Every caller treats a return as proof of delivery — the renderer records the answer as sent, a proactive leg reports it delivered — so swallowing the failure is what makes the gateway claim a message the user never saw. Callers that genuinely tolerate failure (typing, an in-place edit, a command acknowledgement) catch it explicitly at their own call site, which is where the tolerance is a decision rather than a default.
- **A self-authenticating external webhook is exempt from CSRF, method-scoped, and from nothing else**: the Bot Framework Connector sends no `Origin` and no `Referer`, and `check_origin` has no configuration that admits a no-Origin non-loopback POST, so without the exemption the route 403s before its own JWT gate can run. The exemption covers `POST` only, leaves `host_validation_middleware` untouched, and is sound precisely because the handler ignores cookies — the threat CSRF addresses does not exist on it. **The set holds exactly one path.** `/api/hooks/agent` shares the shape and has the separate token-auth bypass, but is NOT Origin-exempt: skipping the cookie gate and skipping the Origin check are two different grants, no reported failure named the second for that route, and a perimeter exemption is far harder to withdraw once a caller depends on it than to add later with its own cause. Adding a path to that set is a security review, not a copied line.
- **Inbound token validation is never reordered behind body USE**: `on_activity` verifies the bearer token before the activity is acted on, and the replay-dedupe check runs AFTER the `serviceUrl` attestation so an unattested activity cannot consume a dedupe slot. The body IS read and JSON-parsed first, under a byte cap — that is what bounds it — so the guarantee is about dispatch, not about reading. A hardening step added ahead of the token check would make the perimeter the trust boundary instead of the signature.
- **Channel identity is asserted POSITIVELY**: `activity.channelId` must equal `msteams`, never "not some other channel". An Azure Bot resource serves Web Chat (enabled by default) and can serve Direct Line off the SAME endpoint with the SAME credential, and on Direct Line the client composes the `from` object — so a negative test would hand a sender-chosen identity to `allowed_emails`, and would fail open on the next channel Microsoft adds.
- **An approval widget carries a per-prompt nonce, minted from one place**: ACP request ids restart at 1 in every provider process, so a control left in a chat from a previous run names an id that is live again for a DIFFERENT tool. Slack, Discord, Telegram and Teams all mint through `messaging.renderer.new_approval_nonce`, compare with `secrets.compare_digest`, retire the nonce with the prompt, and fail CLOSED when none was armed. Three independent copies of that is how one ends up with a weaker token or none at all — which is the state Telegram shipped in. The session picker's nonce (`PickerRegistry.mint`) comes from the same function: a press on a stale list of sessions is the same hazard, so it is not a reason for a second generator.
- **Session resume has ONE controller and routing machine, not one per channel**: `SessionResumeController` is the only consumer of `ResumeSurface`; it owns eligibility/search delegation, the picker registry, access audit, transcript existence, both conflict checks around the awaited UI settlement, expectation-before-success ordering, the atomic inbound claim, dashboard push, and the final audit. `SessionBinder` owns routing, refusal settlement, and release. Discord and Teams supply address and owner identity, their widgets/cards and exact copy/display redaction, callback parsing, and channel-local replay. The machine is where a mistake routes somebody's transcript into someone else's chat, and its hazard is timing: between the durable record read and the live session-map read a binding can appear, vanish or move, so ONE call returns ONE `RoutingDecision` — where the message runs, the refusal that stops it, and the settlement owed once that refusal is delivered. Two resolver calls with an await between them let the binding change in the gap and the routing check fall through to the conversation's own session, silently. A second copy of either transaction is not a maintenance cost, it is a second chance to get it wrong.
- **There is ONE auto-approve grant, and a channel does not get its own**: every surface that can arm it — the dashboard toggle, `/yolo` on seven channels, and Teams' approval card — goes through `safety_override` via `messaging.commands.run_yolo_command`. A channel-local trusted set is a second grant with its own lifetime, its own audit trail and its own answer to "is YOLO on?", and it has to reimplement the expiry, renewal and auditing the shared helper already owns. Slack's `is_slack_session_trusted` predates this and is the one exception; a new channel follows the seven. It also follows that a control which arms the grant must NAME its blast radius: Teams' button says "Approve + auto-approve", not "Trust session", because the effect reaches every surface until the grant expires.
- **A model-authored label is never interpreted as a command**: an `[OPTIONS:]` chip re-dispatches with `interpret_commands=False`, exactly like a drained queue payload. Display redaction does not strip a leading `/`, so with interpretation on a model that emitted `[OPTIONS: /dashboard | cancel]` renders a chip whose single tap mints a dashboard login credential.
- **An option button is bound to the session that posted it**: Discord and Telegram encode `opt:<index>:<tag>`, where `session_provenance_tag` is a stable 12-hex SHA-256 digest of the posting session key. The raw key never reaches client-visible callback data. A press is checked before busy handling and again after idle/daily rotation; a mismatch or an untagged legacy button fails closed. A valid press against a busy session is refused rather than queued or steered, because those paths retain only bare text and would replay the choice with no tag to validate.
- **Attachment ingest belongs to the frame that awaits the turn**: download after the busy check, in the dispatcher, and unlink in that frame's `finally`. Ingesting at arrival and unlinking there leaves a QUEUED message's prompt naming files that were deleted minutes before the drained turn read them, and the encoder skips a missing path silently. It follows that an attachment-bearing message is never steered (a steer carries text only) and never read as a command (the caption lives in `text`); the queue entry carries RAW descriptors and the drained turn re-ingests them.
- **An outbound refusal is never budget-dropped**: when extraction has already CUT a reference's markup, its refusal line is the only surviving trace of the file, so it is appended unconditionally and the caller chunks. Trading the line for staying inside one message is the one outcome that leaves the user with neither the picture nor a reason.
- **A permanently undeliverable route is dropped, a transient failure is not**: `TeamsSendError` carries the Connector status and only `403`/`404` retire the persisted `serviceUrl`. Keeping a dead route turns every later cron result and mirror leg into a red badge nothing can clear; dropping one on a hiccup makes an outage look permanent.
- **An SSRF vet checks the RESOLVED address, not only the name**: a name blocklist cannot see that a public name an attacker controls points at `127.0.0.1` or `169.254.169.254`, and a wildcard-DNS host needs no zone control at all. Resolution goes through one seam, refuses if ANY answer is private/loopback/link-local/reserved, refuses on failure, and runs on every redirect hop. The residual gap (rebinding between vet and connect) is stated rather than implied away.
- **A routing reference is durable, and losing it never blocks delivery**: the Bot Framework exposes no lookup for a conversation's `serviceUrl`, so `teams/service_urls.py` persists it. Loading is lazy and off-loop (never the boot path), every read failure degrades to the in-memory map, a non-`https` row does not survive a reload, and an identity row whose conversation did not survive is dropped rather than advertising a target with no route to it.
- **A turn that produced text but landed none of it is a FAILURE**: `DiscordRenderer.delivery_failed` is "seals were attempted AND none landed", and the dispatcher records `record_failure` rather than `record_success` when the turn accumulated text and that observable is true. A revoked token or a dropped network fails every send while the turn still returns its text, so filing it as a success hides the outage behind a healthy success rate and leaves the transcript claiming a reply the channel never carried. Deliberately not "any send failed": one failed length rotation whose retry succeeded still reached the user. A muted conversation runs a `SilentRenderer`, which attempts no send and so never reports one.
- **A cron run notifies ONE surface**: a job belongs to the conversation that scheduled it, so when the job pins no `channel` and `_deliver_cron_to_channel` reports a DELIVERED send to the origin channel, the Slack owner-DM leg stands down. A pinned `job.channel` keeps its Slack delivery, and a Slack-origin, dashboard-origin or origin-less job keeps Slack too, which is every job an install carries today. The stand-down is gated on that send's own return value, never on a predicate answering whether it would have worked, so a governance refusal or a wire failure falls through to Slack instead of dropping the run.
- **Transport shutdown is quiescent**: a client that fast-acks inbound work in background tasks cancels and awaits those tasks before closing their shared network session or returning from shutdown. Teams owns this ordering in `TeamsClient.close()`, and `DiscordClient.close()` cancels and gathers `_handler_tasks` before closing its `ClientSession`, so a gateway teardown cannot leave a turn unwinding against an already-closed session, which surfaces to the user as a reply that silently stops mid-stream rather than as a shutdown.
- **An inbound file fetch is host-bound and refuses redirects**: a download whose URL comes from the platform's own event envelope is not a URL we chose, so it is validated before any credential is attached to a request for it: HTTPS, a host inside the platform's domain, the default port, `allow_redirects=False` with an explicit 3xx refusal, a bounded timeout, and off-loop writes. Redirects matter specifically because aiohttp REPLAYS an explicitly set `Authorization` header across one, so following a redirect would bounce the credential to an arbitrary host and the host check would have been true only of the hop that did not carry the bytes. Slack's `download_file` is the case where this is load-bearing (it sends the bot token); `discord/client.py::download_attachment` guards its credential-free CDN fetch the same way.
- **A reconnect cannot hot-loop on an accept-then-close edge**: a connection must live at least `_MIN_HEALTHY_CONN_SECS` for its CLEAN close to reset the backoff counter, so a repeating immediate close stays on the exponential curve. Webex, WeCom and Discord all carry this guard. Without it a clean-disconnect branch that resets the attempt count makes the backoff curve unreachable and nothing bounds the request rate, and Discord bans an identity for 10 minutes after 10,000 invalid requests, so the cost is the channel, not just CPU.
- **Session keys are namespaced**: every key is `channel_type:conversation_id`; only bare legacy Slack `thread_ts` keys are shimmed, via `canonical_key`/`legacy_key`.
- **Runtime identity follows the current turn**: every channel dispatcher passes its trusted transport name as `runtime_source` to `ContextBuilder.build_message`; the shared `drive_turn` pipeline uses `ChannelTurn.channel_type`. A cross-surface resume keeps its original stable session key for conversation continuity, but `[RUNTIME]` names the interface carrying the current message. Follow-up turns refresh the marker because the one-time session context may describe an earlier surface.
- **Channel dashboard visibility is immediate**: after the first successful turn of a Discord, Telegram, Webex, Teams, WeCom, Weixin, or Feishu-owned session is persisted, the dispatcher triggers the channel-slot reconciler immediately when `dashboard.surface_channel_sessions` is enabled. `DashboardState.register_channel_transport` injects the dashboard state into the bound dispatcher; the lifetime 30-second reconciler remains the recovery path, but the normal first-turn path does not wait for it. Turns that resume an existing `dashboard:` session skip this step because that session already owns a slot.
- **An owner notification is not Slack-only**: `dashboard/server.py::_dm_owner` prefers the owner's Slack DM and falls back to registered channel transports (`_notify_owner_channels`). It used to no-op entirely without Slack, so an expiring unattended grant was invisible on a Teams-only, Discord-only or Telegram-only install — silence about a security grant lapsing is exactly what the notice exists to prevent. Fallback, not addition: an operator with Slack gets one notice, not one per channel. Reachability is the transport's OWN answer, so this can only reach a destination that channel already authorized. **And a channel must be able to NAME the owner: exactly one configured target, or nothing.** The notice carries the operator's own security state, while an allow-list is a list of people permitted to talk to the agent — not a claim that any one of them is the operator. With several configured targets there is no unambiguous owner, and sending to the first reachable one hands one allow-listed human another's auto-approve state; the count is over ALL configured targets, because a three-person allow-list with one learned route is still a guess. Same premise as `/sessions`' owner-only rule. Per-identity authority within an allow-list would let this deliver on a multi-person install; it does not exist yet on any channel.
- **The proactive PRODUCERS started Slack-shaped, and the parity claim tracks how far that has moved**: `api_send_message` (the LLM-facing `send_message` tool) began with exactly two legs — the origin dashboard slot and `state.slack_client` — and `file_send` still posts to the Slack upload route. The tool's own explicit addressing now exists for every registered channel and does consult `state.channel_transports`: `channel_type` (+ optional `target_id`) for the conversation the session belongs to, and `session="<channel>"` for that channel's configured owner. See § Proactive sends. What remains Slack-only is the shape of `channel`/`user`/`thread_ts`/`unfurl_*`, whose allow-list, threading and unfurl semantics are Slack concepts, and `file_send`'s upload route. A cron result also still reaches a non-Slack channel when its origin slot is MIRRORED there (`/link`).
- **Configured outbound targets are transport-owned**: `MessagingTransport.configured_targets()` returns opaque `ConfiguredChannelTarget` records for the user-configured destinations a dashboard session may link to, including an explicit unavailable reason when a protocol needs prior inbound state or cannot send proactively. `resolve_configured_target()` revalidates the selected opaque id at the side-effect boundary and resolves it to `(conversation_id, thread_id)`; the browser never supplies an unchecked platform conversation id. Discord exposes configured users and threads, and fail-closes thread resolution unless Discord still reports the allow-listed id as an actual thread rather than a normal shared guild channel; Telegram exposes configured DMs; Webex exposes configured DMs plus, when `webex.allow_group_rooms` is on, each space in `webex.allowed_room_ids` as a `room:` target — and `resolve_configured_target` re-validates a `room:` id against BOTH the switch and the list, because an advertised target id travels through the browser and the LLM (it is the `target_id` an MCP send may name) and the config can narrow after one was minted; Weixin exposes allow-listed DMs plus authorized peers learned under its open policy; Teams destinations become available after an authorized inbound activity supplies a conversation/service URL; and WeCom advertises its allow-listed userids plus, under its allow-all policy, the peers it has learned — each either offered or listed with a reason, because `aibot_send_msg` needs no token but the platform only delivers into a conversation the user has already written to. Feishu destinations are visible but unavailable because replies are anchored to an inbound message (no proactive DM in v1).
- **Configured-target egress is governed at every yield boundary**: the dashboard mirror-link endpoint enters the shared fail-closed `channels` governance ladder before resolving an opaque target (resolution may itself open a remote DM), rechecks before the initial link message, and rechecks before each historical-context message. A profile that narrows after transport startup therefore stops both target resolution and all subsequent sends.
- **`/link` and `/unlink` are one pair with one location**: `rebind_conversation_location` claims what `release_conversation_location` frees, and both take the channel's single `_origin_mirror_link()` value — the release matches an occupied location by VALUE, so a second spelling of "this conversation" lets it miss the binding the bind wrote. Inside the rebind the **claim goes first**: `batched_save` writes on the way out even when the block raises, so an opt-out withdrawal ordered ahead of a refused claim would persist for a link that never happened and silently turn mirroring back on.
- **A proactive send names its destination and fails closed on it**: `send_message`'s Slack fields and its `channel_type` are mutually exclusive families, a refused channel delivery never falls through to Slack, and every refusal is audited and reported (502 `channel_delivery_failed`) rather than absorbed into a dashboard notification. The destination comes from gateway-owned state — a cron's job `session_key`, or the kernel-attested `X-Session-Key` header — never from the request body. See § Proactive sends.
- **A capability the driver accepts, the shared pipeline must forward**: `drive_turn` hands `TurnDriver` every rung a forked dispatcher does, including `auto_approve_session`. Omitting one is not a missing feature but an ASYMMETRY, and it fails silently in the direction that LOOKS safe and is merely useless: the field existed on the driver while the pipeline never passed it, so an operator's `/yolo` grant — taken from the dashboard toggle or Telegram's `/yolo`, both of which write the same process-global grant — was inert on every channel riding `drive_turn`. Discord's fork does not pass it either, and has no `/yolo`; Telegram's does. The predicate is read PER REQUEST, never captured at turn start, so a mid-turn revoke takes effect on the next tool. The PreToolUse `tool_gate` still runs first, so a hard deny can never be overridden by it.
- **A channel conversation binds itself as origin AND mirror, every turn**: a dispatcher supplies `ChannelTurn.origin_conversation` and `drive_turn` records it via `set_origin_link` (so unattended output — the auto-compact notice — has a target) and `bind_origin_mirror` (so a turn later taken from the dashboard comes back to the chat). Re-asserted on every turn, because a restart, an unlink elsewhere, or a rival claim can REMOVE the binding and none of them repoints one; a binding already aimed elsewhere is therefore left alone. Guarded as a pair at the call site: losing the mirror costs a convenience, while raising there costs the user the answer they are waiting for, and this is the widest call site in the codebase. A channel that omits the field keeps its conversations unmirrored, which is why the roster in `autonudge._CHANNEL_KEY_PREFIXES` is narrower than `CHANNEL_SESSION_NAMESPACES` — a loop with no bound conversation fires into nothing while reporting itself healthy.
- **A proactive send addresses an OPAQUE target, never a platform id**: `POST
  /api/send-message` reads `channel_type` as the transport and `target_id` as the
  optional destination on it — `channel_type` alone means the conversation the
  SESSION already belongs to (`_deliver_to_channel`), and the pair names an
  explicit configured destination (`_send_to_channel_target`), so `target_id` is
  what selects the addressed leg and a `target_id` with no transport to resolve it
  against is the one under-specified combination. The addressed leg
  resolves the id through that transport's `resolve_configured_target`, which
  re-applies the channel's own allow-list at the side-effect boundary. That
  matters because the endpoint is reachable by the LLM and the id travels through
  the browser: a raw conversation id would let a caller name any destination, and
  a config that narrowed after the id was minted would not take effect. Four gates
  run, all fail-closed: a registered transport (membership, never `!= "slack"`),
  `supports_proactive_send`, the `channels` governance chokepoint, then the
  allow-list. The `send_message` MCP tool vets the transport it NAMES rather than
  the literal "slack", because the scope is per-transport. Every non-2xx body
  carries a machine-readable `code`, since backend strings have no catalog path.
- **Own-channel vs. mirror**: `ChannelLink` models a session's own inbound channel only; the dashboard→Slack mirror binding stays in `SessionMap.get/set_slack_link` (guardrail G3). The generalized channel-neutral outbound mirror (`SessionMap.set_mirror_link`) stores a `ChannelLink` under the `mirror` slot for non-Slack channels, still distinct from the session's own inbound link.
- **Managed-MCP session-key resolution**: every turn-running surface publishes `session_pid_<pid>.txt` (with an HMAC-SHA256 sidecar) through the single shared helper `messaging.identity.publish_turn_identity` (which calls `session_pid_sig.publish_session_pid`), keyed by the session's kiro-cli host PID, so the gateway's ancestor PID-walk resolves the caller's `X-Session-Key`. One writer is called by the dashboard, native Slack, and every shipped channel transport-dispatch surface: Telegram (DM + forum), Discord, Slack, Webex, WeCom, Teams, Weixin, and Feishu (through the shared `drive_turn`). Any surface that omits it makes every session-keyed managed MCP tool (`learn_add`, cron management, …) fail with HTTP 400 `missing X-Session-Key` from that channel's turns; the identity-topology test guards every dispatcher against regressing.

## Testing conventions

The extraction is gated by a **golden-transcript** harness (`test/test_slack_golden_transcript.py`): a `RecordingSlackClient` captures the ordered sequence of Slack-render operations the native `handle_message` emits for a scripted `ScriptedProvider` event stream, establishing the baseline the `TurnDriver` + `SlackRenderer` rewire must reproduce identically. Layer contracts and the Slack impl have dedicated suites: `test_messaging_transport.py`, `test_messaging_driver.py`, `test_messaging_privacy_mode.py`, `test_messaging_auto_title.py`, `test_slack_renderer.py`, `test_slack_transport.py`, `test_slack_transport_dispatch.py`, `test_slack_transport_integration.py`. Providers are always mocked (scripted event streams) — never spawn a real kiro-cli process. `test_messaging_import_purity.py` is the gate on the one-way dependency, over every edge including function-local imports, so a new neutral module is covered by it automatically.

## Slack settings API

Three dashboard-only endpoints back the `/settings/channels/slack` panel (legacy `?tab=channels&channel=slack` and `?tab=slack` links redirect there). They are
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
Outbound is REST v10 (send/edit/typing/reactions/interaction acks) through one
ladder that spends the rate-limit accounting Discord hands back on every
response: a bucket whose last response reported `Remaining: 0` is waited out
before the next call on it rather than earning a 429, a global 429 holds every
route except the interaction routes Discord exempts, and an invalid-request
breaker trips well below Discord's 10,000-per-10-minutes IP-block ceiling and
refuses to send during a cool-off, because that block costs the whole channel
rather than one call. Failures are classified (`DiscordApiResult`): a 4xx other
than 429 is permanent and never retried, while 429/5xx/timeout/connector
failures retry within per-class budgets. A caller that only checks truthiness
still reads a failure as falsy, so the classification is additive. Malformed
(non-JSON) response bodies degrade to an error result and never propagate into
rendering. Attachments ride the same ladder over multipart
(`send_message_with_files` / `edit_message_with_files`, see "Discord's upload
half" above). No public
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
an approved sender and either an exact `discord.allowed_thread_ids` match or an
exact `discord.allowed_channel_ids` match. An allowed channel message is never
handled in the shared channel itself: with `discord.auto_thread` enabled, the
transport creates a public thread from that message and dispatches the turn
there. That runtime widening is SEL-audited as a GRANT
(`discord_transport.auto_thread` / `thread_authorized`), not merely on refusal: a
new authorized disclosure boundary appears at runtime, and without the record the
log shows the turns that ran in the thread but never the decision that admitted
it. The allow-set is deliberately unbounded, because each entry is a thread an
already-approved user created, and evicting one would silently stop answering in
a conversation they are still holding. Existing thread IDs still require a REST channel lookup confirming
Discord type 10/11/12. An approved thread is a shared disclosure
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
under the 2000-char cap, splitting ordinary text with the shared
`split_markdown_safe` (at 1900 less 100 characters of chip/footer headroom)
and holding local-image markup for secure multipart extraction at the semantic
steer/final seal. It rotates messages at the shared driver's structured steer
boundaries with quote chips, renders trailing `[OPTIONS:]` as button action rows
(`opt:<i>:<tag>`, label recovered from the component at interaction time; `<tag>`
is `session_provenance_tag()` — a 12-hex SHA-256 digest of the session key the
buttons were rendered for), and posts
Approve/Deny buttons for interactive tool approvals. Approval `custom_id`s carry a
per-prompt random nonce (`a:<request_id>:<nonce>:<1|0>`) validated at
resolution: ACP request IDs are reusable across provider/gateway restarts, so a
stale button without the matching nonce fails closed. The decision window
denies by default on timeout and retires the nonce with it.

**Option-press provenance.** Discord replays old components indefinitely, so an
option press dispatches only when the session the conversation currently
resolves to (via the resume binding, else the native DM session) is the one the
tag names — a press carries a non-empty tag, and the tag implies resume routing
even though the label never executes as a command (button labels are
model-authored turn content, same rule as the queue drain). The tag is checked
before the busy path — a press against a busy target is refused, never
queued/steered, because the drain replays items tag-less — and re-checked after
idle/daily rotation so it cannot run under a generation the tag never named. A
mismatch (the chat was rebound or `!new`'d since the buttons were posted) and an
untagged pre-provenance button both fail closed with a refusal naming the
remedy; queue drains and AutoNudge fires dispatch untagged with commands off and
keep their native-session affinity.

Every turn closes with a **one-line footer** as Discord subtext (`-#`) on the
final segment, rendered by the shared `format_turn_status` (see "Turn-status
surfacing" above): elapsed time plus the context-usage chip. The clock starts when
the renderer is constructed rather than at `on_turn_start`, because the cold start
that spawns and handshakes a session is time the user waited. The usage figure is
read at turn END from the session provider the dispatcher hands over
(`bind_context_source`), so the chip reports the window as the user leaves it, and
an unbound or failing provider renders no chip rather than a reassuring green one.
It rides the last segment instead of its own message (one turn, one bubble, and
Discord charges rate budget per message), lands on the placeholder when a turn
produced no text, and is dropped rather than truncated when the segment leaves no
room: a clipped answer costs the user more than a missing timing line.

Two `discord` config toggles shape what else is rendered. Both are re-read from
the live config per turn (`_render_config`), not taken from the boot-time
snapshot, because an operator who turns the reactions off in the dashboard expects
the next message to be quiet rather than the next restart; a failed load keeps the
shipped defaults instead of failing the turn, since neither toggle is a security
control.

- **`reactions_enabled`** (default `true`) is the phase ladder above. Off, no
  reaction is added at all: the renderer never arms a ladder, so there is no
  progress emoji, no stall mark and no terminal 🦞/😱. It is only one of the
  preconditions, so the ladder also stays off for a transport that does not
  declare `reactions` or a turn with no user message to decorate (an injected
  turn).
- **`show_thinking`** (default `false`) surfaces the model's reasoning. On, the
  accumulated reasoning posts ONCE as its own `-# 💭` subtext message ahead of the
  answer, capped at a preview length because subtext is grey and unscannable in
  bulk (the full reasoning stays in the dashboard Activity panel); its own message
  rather than the answer bubble, which is edited in place for the whole turn and
  would overwrite it. Off, reasoning is never even accumulated. Either way the
  ladder still moves on reasoning, because the reaction reports what the agent is
  doing regardless of whether the words are shown.

**No send may notify anyone.** Every message body is LLM- or tool-derived, so
`client.py` puts `allowed_mentions: {"parse": []}` on the JSON body of every
message create/edit through one shared `_message_payload` builder, and on every
interaction callback. That placement is the point: the renderer's text-level
defang (`_DISCORD_MENTION_AT_RE`, a zero-width space after `@`) covers only the
sites that route through `_redact_transformed`, while the option-choice echo, the
help card, the queue receipt, the threshold notice, the session picker and every
proactive delivery do not, and a text guard applied per call site is one new
send path away from being wrong. `replied_user` is omitted (it defaults off): a
reply already lands in the conversation its recipient is reading.

**Display-form redaction is a floor, not a step on the upload path.**
`TurnDriver` redacts the LITERAL form of every chunk upstream; the display pass
(`redact_for_display`) exists for the credential that is invisible until Discord
renders the markdown away. Both renderer sinks therefore run it
unconditionally (`_stream_live` on every live frame and `_seal_current` on every
seal), where gating it on `_uploads_enabled()` had left a restricted session, an
unset upload root, a channel with `files_outbound` off, and **every length
rotation** sending model text the display pass never saw. `_extract_uploads`
applies the pass itself to the text it rewrote, since removing image markup is
one of the transforms that can reassemble a credential.

**Application (slash) commands.** Discord's analogue of Telegram's
`set_my_commands`. `discord/commands.py` owns one ordered `COMMAND_SPEC`
catalogue feeding BOTH the `!help` card (`build_help_text`) and the registration
payload (`application_command_payload`), so the two cannot drift.
`client.register_application_commands` bulk-overwrites via
`PUT /applications/{application_id}/commands`, the whole set every time, which is
what makes it safe to call on every start rather than diffing, using the
`application_id` READY supplies. A row that breaks Discord's own constraints is
skipped rather than sent, because Discord rejects the ENTIRE array on one bad
row. `gateway.py` fires it as a background task rather than awaiting it: the
product is discoverability alone, the gateway boot path must not grow an awaited
network step, and the `!` text commands are the floor if it fails.
`contexts: [GUILD, BOT_DM]` replaces the deprecated `dm_permission`. `queue` and
`steer` are deliberately absent from the menu: they are message PREFIXES, and a
menu tap sends the bare token with no message body to act on.

The client accepts interaction types `APPLICATION_COMMAND` and
`MESSAGE_COMPONENT` (`_HANDLED_INTERACTIONS`); PING, autocomplete and modal
submit are dropped at the dispatch boundary, since each needs its own callback
shape inside Discord's ~3s deadline and a handler that received one without
knowing how to answer would leave the client spinning. Command replies use
callback type 4 with the EPHEMERAL flag (`1 << 6`), because a slash command is invocable
from an approved thread every member can read, and these replies carry runtime
state or a login link. A command is **never** pre-acked as a component:
`DEFERRED_UPDATE_MESSAGE` is component-only, and the one permitted first response
is the command's only route. It also runs the `channels` governance gate BEFORE
responding rather than after, unlike the button path; a governance check slower
than the callback window therefore makes the command visibly fail, which is the
correct direction for a fail-closed gate.

`status` is a single-reply command whose handler takes a `ReplyFn` sink, so the
text and slash surfaces share one body and differ only in where the reply goes.
It reports `Stats().summary()`, the same source Slack's `/kirocrew status` uses,
so the two channels cannot report different numbers for one gateway.

**Discord deliberately offers NO operator-authority command**, so there is no
`yolo` and no `dashboard` here even though Slack and Telegram carry both. One
grants gateway-wide auto-approve and the other mints a bearer credential for the
whole dashboard; `discord.allowed_user_ids` answers "may drive the agent", which
is a weaker claim than either. Keeping them off this channel means the narrower
surface needs no owner-disambiguation rule, no DM-only refusal, and no
credential-issuance audit path -- there is nothing to gate. Do not add them for
parity's sake: parity is measured against what a channel should be able to do,
not against the widest channel's command list.

`model` is button-only and does NOT take a sink: its buttons must live
on an editable channel message, which an ephemeral response is not. Its
`custom_id` is the INDEX (`m:<i>`), never a model id: a custom_id caps at 100
characters and Discord replays old ones indefinitely, so the dispatcher's
TTL-bounded picker registry resolves the index against the exact list it posted
and refuses an expired, evicted or already-consumed one rather than applying a
model from a stale advertisement.

### Resume-binding expectations (`messaging/resume_expectation.py`)

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
  (true unless the request is direct-local), and the two render toggles
  `reactions_enabled` / `show_thinking`. Never returns a raw secret.
- `PUT /api/discord/config` — requires a direct-local request (loopback peer
  AND no forwarding headers); remote gets 403. Validate-first/commit-last.
  New tokens must match the three-segment bot-token shape (an accidental
  `Bot ` Authorization prefix or `DISCORD_BOT_TOKEN=` env line is stripped)
  and are verified against Discord `GET /users/@me` before storage; rejection
  returns 400 and writes nothing, network failure saves with
  `verify_warning`. `bot_token_clear` must be a strict boolean.
  `allowed_user_ids`, `allowed_thread_ids`, and `allowed_channel_ids` accept
  numeric snowflake strings only; `auto_thread`, `reactions_enabled` and
  `show_thinking` are strict booleans. Secrets
  land in `config_dir/.env` (atomic 0600) with `os.environ`
  synced; non-secrets go to
  `config.json` under `discord`. Every field except the two render toggles is
  boot-read, so `restart_required` is true for any other actual change and
  `_DISCORD_LIVE_FIELDS` holds those two out of it: the dispatcher re-reads them
  per turn, and promising a restart the user does not need is how a settings page
  trains people to restart for everything.
  Setting OR clearing the token also purges the legacy `discord.bot_token`
  field from `config.json`, and the commit order is config.json FIRST then
  `.env`, matching the Telegram and Webex saves: the gateway falls back to that
  field when `.env` is empty, so a crash between the two writes would otherwise
  resurrect a revoked credential on the next restart, and the copy sits in
  agent-readable `config.json`. Both writes go through `asyncio.to_thread`:
  the atomic write fsyncs, and the owner-only lockdown shells out to `icacls`
  on Windows, neither of which may block the gateway loop.

## Telegram channel

The channel's transport, forum routing and mid-turn machinery are described in
the sections above; what follows is what is specific to its rendering and its
Bot API surface.

### Telegram's read-only session search

`/sessions` and its singular `/session` alias remain direct-message-only,
single-operator, and read-only. With no argument they use the shared recent-sessions
collector, preserving the ten-row newest-first list, live marker and agent label. With
search words they call `ConversationLog.search_sessions` off the event loop, so title
and message-content matching, AND-term semantics, CJK handling, recency weighting,
and ranking are the same as dashboard history search rather than a Telegram-only title
filter. Incognito and temporary rows are excluded after a bounded over-fetch, before
the ten-row display cap, so private content cannot leave a match trace and private hits
cannot starve later public results. Live markers restore dashboard stems through the
shared history-key helper and resolve irreversible channel filename folds through
`SessionMap`, never by guessing colon positions. Both successful and failed reads emit
`telegram.sessions_data_access`; displayed query text, titles, agents and errors are
redacted and bounded. Search results contain no callback controls and direct users to
`/kirocrew dashboard`, so this capability does not claim inbound session binding
before Telegram owns the resume transaction and routing path.

### Telegram's upload half (`telegram/`)

The second channel wired onto `outbound_files.py`, and it differs from Discord in
one structural way: **photos ship as their own message after the text seal, never
as a caption.** A caption is capped at 1024 characters against the message's
4096, `sendMediaGroup` carries no `reply_markup` at all, and the answer has
already been rendered through this channel's HTML / Rich-Message / table
machinery — folding a truncated second copy into a caption would be strictly
worse than one clean bubble followed by its pictures. The picture send is
`disable_notification`, because the answer bubble already pinged.

- **Named ceilings fed in as budgets.** `client.py` declares
  `TELEGRAM_MAX_PHOTO_BYTES` (10 MB — the multipart ceiling, which is the one that
  binds for a local file; by-URL is 5 MB and by-`file_id` unlimited),
  `TELEGRAM_MAX_MEDIA_GROUP` (10) and `TELEGRAM_MAX_TOTAL_UPLOAD_BYTES` (25 MiB,
  the aggregate that bounds resident bytes — ten photos at the per-file ceiling
  would be 100 MiB). `_api_multipart` sits beside `_api` and both run through one
  `_api_request`, so the single 429 `retry_after` back-off exists once. The body is
  a FACTORY, rebuilt per attempt, because an aiohttp form is consumed as it is
  written. Every `attach://` descriptor is built where its part is added.
- **Only semantic seals extract, once.** `_rotate_on_length` holds the earliest
  complete-or-still-arriving reference and its suffix in the live tail
  (`protected_ref_spans`, off-loop), so a length cut can never bisect
  `![alt](path)`; length-sealed chunks pass `extract_uploads=False`. Live frames
  run `hide_local_refs` so no filesystem path flashes and then vanishes.
- **Recovery restores the REFERENCES, not the segment.** Discord sends text and
  files in one multipart call, so its recovery re-posts the whole thing; here the
  text bubble has already landed, so re-posting the source would deliver the
  answer twice. The markup is rebuilt from each `OutboundFile`'s own alt and path
  and sent as one short follow-up, display-redacted.
- **Two gates, one of them not yet reachable.** `files_outbound` is read before
  extracting. The second is `messaging/upload_gate.uploads_restricted`, which
  denies an incognito or temporary dashboard session — and today it cannot fire
  on this channel: it keys on a `dashboard:` session key, and Telegram derives
  its key from the route alone (`supports_session_resume` is False), so no
  Telegram turn ever carries one. It is wired anyway, because the gate is shared
  with Discord where it DOES fire, and because the day inbound resume lands here
  the ceiling has to already be in the path rather than be remembered.

### Telegram's display-form redaction

`TurnDriver`'s redaction is byte-level and runs BEFORE this renderer introduces
markup, so it cannot see a credential that markup will REASSEMBLE:
`redact_credentials("AKIA**IOSFODNN7EXAMPLE**")` matches nothing because the `**`
sits inside the key, and `_md_to_telegram_html` then emits
`AKIA<b>IOSFODNN7EXAMPLE</b>` — which Telegram displays as an intact access key.
A link (`[AKIA](https://x)REST`) and a zero-width character between the halves are
the same hazard. `messaging/display_safety.redact_for_display` canonicalizes to the
rendered form and scans both it and the literal; it runs at the two sinks — the
live plaintext frame and `_seal_text`, ahead of the HTML, Rich and plaintext
branches alike. A redaction can push text past the budget that sized it; the seal
re-measures and re-splits, and losing formatting to keep a rendered secret
redacted is the intended direction of that trade.

### Telegram's stall marks, and why it has no phase reactions

Slack tracks turn phase with a debounced reaction on the user's own message. That
does not port: a bot holds exactly ONE reaction per message (setting is a replace,
not an add), `setMessageReaction` accepts exactly **73** emoji — with no globe,
wrench or brand mark among them, and `✅`/`🚀`/`⏳`/`🤖` all absent — a chat's own
`available_reactions` can narrow that further at any time, and Telegram's rate
limit is per CHAT, shared with the streaming edits the answer already spends. So
the phase indicator would compete with the answer for the same budget.

What Telegram lacked was not "working" (the typing indicator and the `🔧 {tool}…`
footer say that) but "working, and nothing has moved in a while". Two marks ride
the footer the renderer was going to edit anyway — `🥱` at 15s, `😨` at 45s, the
Slack controller's own thresholds — read from the clock rather than latched, so
they clear themselves when output resumes. They never reach a sealed message.

`client.REACTION_EMOJI` is the validated 73-member set, spelled without U+FE0F to
match the Bot API reference; seven members are documented bare while every keyboard
emits the VS16 form, and the two major Python libraries disagree about which three
carry it, so `normalize_reaction_emoji` strips it on both sides of the membership
test. `set_message_reaction` refuses an off-list emoji locally with a log line
instead of spending a round-trip on a guaranteed 400, and returns whether Telegram
accepted it — passing the global list is necessary but not sufficient.

### Telegram's option-button provenance

Trailing `[OPTIONS:]` choices carry `opt:<index>:<tag>` in `callback_data`, with
`<tag>` produced by the shared `session_provenance_tag` from the renderer's resolved
session key. The label remains button text rather than callback data, keeping CJK and
emoji labels out of Telegram's 64-byte payload budget; at the 25-button ceiling the
largest payload is 19 ASCII bytes.

The callback retires the keyboard, rejects an untagged legacy button, and re-dispatches
the recovered label with command interpretation disabled. The dispatcher compares the
tag before reading busy state, refuses every tagged press while that session is busy,
and compares it again after idle/daily rotation. A button posted before `/new`, an
agent switch, or an automatic generation rotation therefore cannot inject its
model-authored label into the replacement conversation, and a busy queue cannot strip
the evidence needed to validate it later.

### Telegram's approval nonce

`callback_data` is `a:<request_id>:<nonce>:<1|0|t>` and the nonce is what a press is
actually validated against; the key it is filed under is `session_key:request_id`,
and neither half of that is unique over time. ACP request ids are REUSABLE — a
provider or gateway restart resets the sequence — while the conversation generation
folded into the session key only changes on `/new` or an idle/daily rotation. So a
provider restarting mid-conversation issues request id 1 again, and a button still in
the chat's scrollback from before the restart carries that same id. Without the nonce
that press resolves a prompt for a tool the user never read: on Approve an unrelated
tool runs, and on Trust the conversation also gains standing auto-approve that
spawned subagents inherit.

`register_nonce` mints 8 bytes of `secrets` entropy per prompt (16 hex chars),
`resolve_global` and `is_pending` both refuse on a mismatch via `compare_digest`, and
the nonce is retired in the decider's `finally` alongside the future — a nonce that
outlived its prompt would re-open the window on the next reused id. All three buttons
of one prompt share its nonce, because they are one decision point and a Deny press
has to be able to retire the prompt an Approve press could have taken.

Budget: Telegram caps `callback_data` at 64 BYTES. The fixed parts cost 21, leaving 43
for a request id. A button rendered before the nonce existed has no nonce segment, so
the right-to-left parse leaves part of the id where the nonce belongs and the compare
fails closed, which is the correct answer for a press from an earlier process. Same
mechanism as Discord's (see above); Slack still lacks it, and its `block_id` has room
for the same fix.

### Telegram's durable `getUpdates` cursor

Calling `getUpdates(offset=N)` is ALSO the acknowledgement for everything below
`N`, so an in-memory-only cursor means a restart re-requests from 0 and Telegram
redelivers every update the previous process never confirmed — the user's last
messages arrive a second time as fresh turns. The cursor is persisted to
`$KIROCREW_HOME/routing/telegram_offset.json` (atomic write, off-loop, written only
when it moved), on the same reasoning as the iMessage watch cursor.

**Under `routing/`, which is a keystone leaf, and that is not incidental.** The same
property that makes the cursor useful makes it dangerous to leave writable: an
offset is an acknowledgement, so a plausible-looking large value makes the gateway
skip every queued and future message, and it survives the restart that would
otherwise clear it. An agent that could write the file could therefore switch off
the operator's own Telegram intake, durably and silently. It shares the directory
with the Teams routing store because they are two shapes of one control — where a
message GOES, and which messages are SEEN — and the DIRECTORY is what `security`
registers, since `atomic_write` publishes through a temp sibling in the same parent
and a file-name leaf would leave the pre-rename window open. A pre-keystone cursor
in the data-home root is DELETED rather than migrated on the next start: a cursor at
a writable path is exactly the "cursor we cannot trust" that `_load_offset` already
answers 0 for in five other cases, and the cost of dropping it is the one bounded
replay that method documents as the accepted degradation.

What is persisted is a **low-water mark**, not what the poll observed. `_in_flight`
holds every `update_id` handed to a handler; `_persistable_offset()` returns the
observed cursor when nothing is in flight and the oldest in-flight id otherwise.
Resolution happens in the handler's `finally`, so it covers refusal and exception
as well as delivery — all three are terminal, and a cursor advancing only past
SUCCESS would replay every deliberately-dropped update (an unauthorized sender, a
sticker) on every restart forever. An album's ids ride with the merged message and
resolve as a unit, since replaying half an album delivers the same photos under a
caption that no longer matches. `_get_updates` writes nothing; the polling loop
persists once the whole batch is registered. Writes go through `_persist_offset`,
which is serialized by a per-loop lock and refuses a value that is not an advance:
two turns finishing hand two writes to the thread pool, nothing orders those
threads, and a regressed file re-answers turns that were already answered.

**This bounds duplicate replay, NOT loss.** `getUpdates(offset=N)` is Telegram's
own acknowledgement and there is no call to un-confirm, so once the loop polls
again an in-flight update is gone server-side whatever the local file says, and a
crash mid-turn loses that message. Closing that needs either inbound serialized
behind turn completion (a long turn would then block `/stop`) or the update PAYLOAD
stored durably before it is confirmed, which is a persistent inbound queue and a
separate feature. Every unusable file (absent, unreadable, non-JSON, wrong shape,
negative) reads as 0, which is exactly the pre-persistence behaviour, and a write
failure costs one replay window rather than the channel.


## Telegram settings API

Two dashboard-only endpoints back the `/settings/channels/telegram` panel (legacy `?tab=channels&channel=telegram` and `?tab=telegram` links redirect there). Like the
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
characters — Webex's limit is 7439 bytes. The renderer's own final answer is
chunked by `webex/client.py::chunk_utf8`, which is byte-exact and **lossless**:
the concatenation of its chunks equals its input. That is the property the table
path requires, because an oversized safe-raw grid is chunked here and must
reassemble exactly, and a line-oriented splitter cannot promise it — it consumes
the boundary whitespace (pinned by
`test_channel_table_rendering.py::TestDeliveryFraming`). The accepted cost is that
`chunk_utf8` carries no fence state, so a code fence spanning a chunk boundary
lands unbalanced; trading the grid's exact reassembly for that is the worse of the
two. The fence-safe `messaging.split.split_markdown_bytes` — shared splitter
against a character budget, shrunk until every chunk measures under Webex's byte
cap, with the `chunk_utf8_bytes` primitive for a fragment admitting no clean cut —
is what Webex gets through `chunk_for_transport` on the mirror and proactive legs,
which carry no grid. Single sends are tail-guarded by `truncate_utf8` as a last
resort, so a multibyte-heavy reply is never rejected wholesale by the platform.
Redelivery is handled at the
frame: each processed activity is acknowledged (``{"type": "ack", ...}``) and its
message id remembered in a bounded FIFO, because an unacked activity is redelivered
and a redelivery arriving mid-turn would otherwise be folded in as a steer — the
agent steered by an echo of the instruction it is already following. The reconnect
loop uses exponential backoff with a
minimum-healthy-connection guard so a bad token can never hot-loop.
``client.ready`` (asyncio.Event) is set on connect+authorize and cleared on
disconnect; ``maybe_start_webex`` reports ``connected`` only after
``wait_ready`` succeeds and keeps the dashboard badge truthful via the
``on_state_change`` observer (a disconnect flips it back off with the
reason).

**Security model.** `authorize` is deny-by-default against
`webex.allowed_emails` (lowercased comparison); every denial is SEL-audited.
Self-messages are dropped twice (WS actor email + hydrated `personId` against
the bot identity). `WEBEX_BOT_TOKEN` is on the sandbox agent env denylist.

**The allow-list IS the operator tier, and that is the cross-channel design.** No
shipped channel carries a second, narrower "owner" check: Telegram's `/yolo` and
`/kirocrew dashboard`, Teams' `/yolo` and Slack's `!yolo` are all reachable by any
authorized sender, and Webex matches them rather than inventing a tier one channel
has. So an operator adding a second address grants that address the process-global
`safety_override` grant (`/yolo` — the same one the dashboard toggle and the CLI
drive) and dashboard-link minting. What bounds it is attribution plus audience:
both commands SEL-audit the sender's email, and `/kirocrew dashboard` refuses
outside a direct room so a presigned link is never posted where a space can read
it. A narrower tier here would also silently break the ordinary case of one person
with two addresses; if per-sender capability limits are wanted, they belong in the
governance profiles that already gate every channel, not in one dispatcher.

The ROOM gate is separate from the sender gate, and a group message must clear
BOTH. A direct room is always permitted. A group space is permitted only when
`webex.allow_group_rooms` is on AND the space is in `webex.allowed_room_ids`,
which is deny-all by default — so enabling the switch alone grants nothing. The
gate is positive membership (`room_type == ROOM_DIRECT` / `ROOM_GROUP`), never
`!= "direct"`, so a room type this code has not seen inherits nothing. Group
spaces are off by default because a reply there is readable by every member,
including people the email allow-list excludes; for the same reason a group turn
constructs its renderer with `uploads_allowed=False` (a local file reference keeps
printing the path) and `_handle_dashboard` refuses outside a direct room.

An Adaptive Card press reports no `roomType` on the wire, so the client resolves
it (`_room_type_of`, cached) before dispatch rather than letting the gate
special-case it — a second branch there has to guess, and a guess that admits a
press by room id alone drops every DM press the moment a space is named. The
`denied_room_not_permitted` record carries the room id, which is the one thing an
operator needs from it: a Webex room id is opaque and has no UI that shows it.

A space is its OWN conversation. `_route_of` returns the sender's email for a
direct room and `space:{room_id}` for a group space, and that one string is the
`ConversationState` bucket, the generation seed AND the session-key scope — so
they cannot disagree about which conversation a message routes to. The space is
namespaced `CHAT_TYPE_FORUM`, which is what keeps it out of the `unified` DM
bucket. Routing a space to a participant's DM key would answer their private
history into the room, make a mid-turn DM steer into the space turn, and let
`/new` in the space reset their DM.

In a space Webex only delivers messages that @mention the bot and does NOT strip
the mention, so `commands.strip_bot_mention` removes a LEADING mention of the
bot's own name (matched on a word boundary, so `Kiro` does not eat the `Kiro` in
`KiroCrew`). A mention later in the sentence is content and is left alone.

**Files.** `files_inbound` and `files_outbound` are both live. Inbound: a message
arrives on the `share` verb (not `post`), so the accepted-verb set is what makes
file messages visible at all; `webex/attachments.py` HEADs each opaque
`/v1/contents/{id}` URL for its name/type/size and hands the result to the shared
`messaging.attachments` ingest, which owns caps, classification, signature
sniffing and temp-file ownership. The download honours Webex's anti-malware state
machine — 423 retry-after (bounded), 410 infected, 428 unscannable, all refused
rather than passed `?allow=unscannable` — and refuses a URL outside the API base
so the bearer token cannot be sent elsewhere. The `update` verb fires when a scan
clears and is acted on only once every file reads a safe quarantine state; a
pending scan is deliberately NOT dedup-marked, so the later clearing update is
not mistaken for a redelivery. Outbound: `client.send_file` takes the validated
BYTES from `messaging.outbound_files`, never a path — re-opening the path would
resolve the name a second time and could upload something no gate saw. One file
per message is Webex's limit, so a run of files is a run of messages.

**Adaptive Cards** are Webex's Block Kit analogue (`webex/cards.py`, schema 1.3,
one card per message) and `rich_blocks` gates whether one is attached at all. A
press returns as the `cardAction` verb whose `inputs` map merges the pressed
action's own `data`, which is how the routing key round-trips — the same
mechanism as a Slack `action_id`. Three things make a press safe: the reserved
`data` keys are namespaced so a card input cannot forge a decision; a choice
travels as an INDEX into the choices that renderer rendered, so a crafted press
cannot inject text; and each card carries a per-prompt nonce compared in constant
time, which is what retires a resolved card — Webex refuses to edit a message
carrying an attachment, so its buttons stay clickable forever otherwise. A card
is always accompanied by the text form, which works on its own.

**Geo routing.** A Hydra id names a CLUSTER, and synthesising `us` makes a
non-US-resident org drop every inbound message silently: the REST fetch resolves
nothing and the failure surfaces as no reply behind a green badge. The cluster is
therefore read from the activity's own `target.globalId` and remembered per
connection, and the WDM host is discovered per token from the U2C service catalog
(falling back to the documented US host, so a discovery outage degrades rather
than taking the channel down, and never to a plaintext host — the bearer token
rides these requests). `webex.wdm_base` PINS a **Webex** host for a restricted network. It is
suffix-checked against `*.wbx2.com` / `*.webex.com` / `*.ciscospark.com` over
https and dropped (loudly, falling back to discovery) otherwise, because
`config.json` is agent-writable by design — `security.py` deliberately does not
over-block it — and the bot token rides device registration, so a value from a
prompt-injected `config set` would otherwise POST the token wherever it named. An
outbound proxy belongs in `HTTPS_PROXY`, which the client honours separately. The
same suffix rule is applied to the catalog's own `serviceLinks.wdm` as defence in
depth. Otherwise:
the pin is held separately from the host in use and skips discovery entirely,
because discovery WRITES the host in use, so a pin stored only there is destroyed
by the first successful discovery and the config key silently becomes a no-op
after one connect. An empty value is what means "discover", so no caller may
default it.

**Dispatch + rendering.** Turns ride the shared `drive_turn` / `TurnDriver`
pipeline. `transport_dispatch.py` intercepts the command surface (`/new`,
`/compact`, `/help`, `/stop` + `/cancel`, `/link`, `/unlink`, `/yolo`,
`/kirocrew dashboard`, `/model`, `/sessions`, plus the `/queue` and `/steer`
per-message overrides),
queues or steers mid-turn messages, drains the queue after the turn, runs
`/compact` under atomic `try_acquire`, and posts soft/hard context-threshold
notices as separate proactive messages. `COMMAND_SPEC` in `commands.py` generates
the `/help` card, so the card cannot drift from the parser; an unrecognised
`/token` answers with the card rather than spending a turn having the model
explain it. `/compact` reads the compaction RESULT rather than assuming success —
the ACP client synthesizes a completion whenever text streamed, so a compaction
that reported `timeout` or `failed` used to be announced as done.

**Interactive tool approvals accept a typed reply OR an Adaptive Card press.**
`on_prompt_choice` posts its own message asking for `1` (approve) or `2` (deny),
with an Approve/Deny card attached. The typed reply is PRIMARY, not a fallback:
Webex refuses to edit a message once it carries an attachment, so a resolved
card's buttons stay clickable forever, and the inbound half of a press rides the
undocumented device websocket. The dispatcher intercepts either ahead of the steer
path, because the session semaphore is held for the whole turn so an answer
necessarily arrives while the session is busy. An unrecognised reply still steers,
so a user who ignores the prompt does not lose their message.

The pending-decision registry is channel-neutral (`messaging/approval.py`): a
process-global map keyed `session_key:request_id` because ACP request ids restart
at 1 per session, deny-by-default on timeout, and a timeout also signals
`autonudge.notify_approval_stalled` so an unattended loop deactivates instead of
burning its cycle budget being denied. The card's nonce is minted by that registry
against the pending entry and validated INSIDE `resolve()`, as a precondition:
checking it around the call would approve the tool first and only then discover
the press was stale. A press carrying no nonce or request id fails closed, and
every outcome — honoured, stale, expired — emits a SEL record, because a forged
press leaves no other trace. A channels-governance deny blocks an approve but
still resolves a deny, so a policy that forbids the channel does not strand the
tool request for the whole window. An answer that matches nothing is reported
neutrally ("already answered or timed out"), never as a denial: with buttons the
platform cannot retire, "already answered" is the common case and claiming a
denial would tell a user their approved tool was refused.

The renderer is shaped by Webex's 10-edits-per-message cap: no typewriter
streaming (`streaming=False`). A "🤔 Thinking…" placeholder is posted at turn
start; status frames carry the running tool AND a bounded tail of the answer
buffered so far, so a long agentic turn shows the answer forming instead of a bare
tool name. The budget is split explicitly — status frames get 8 of the 10 edits,
2 are reserved for the final answer, and a failed edit burns the remaining status
budget so the final edit can never race the cap. Frames are paced by a DOUBLING
interval rather than a flat one: a flat throttle spends the whole budget in the
first few seconds and then freezes for the rest of the turn. The final answer
lands as one placeholder edit with a fresh-message fallback plus chunked
follow-ups. A folded steer is noted on that final edit rather than posted
separately. `[OPTIONS:]` choices render as an Adaptive Card of one `Action.Submit` per
choice, capped at `cards.MAX_CARD_ACTIONS` with the overflow numbered into the
body by the shared `apply_options_cap`, so widget and text form ONE list. Card and press agree on button order through
`cards.usable_choices`, the single derivation both read — deriving it twice would
let one dropped blank choice shift every index after it, and the button would
answer with its neighbour. What the card offered is published to
`cards.LiveChoices`, owned by the DISPATCHER: the card is the last thing a turn
sends, so a renderer-owned map would be gone before any press arrived. Entries are
one-shot and expire by replacement, because the platform cannot retire the buttons.
A press re-dispatches the label with `interpret_commands=False` — the label is
model-authored, so a `[OPTIONS: … | /yolo on]` trailer must not become a command —
and the choice is echoed first, since a press leaves no trace in the room. An unterminated `[OPTIONS…` fragment is hidden in a STATUS FRAME only, where text
is still arriving and it may be a marker mid-flight. The sealed answer keeps it:
this renderer buffers a whole turn and sends once, so by `on_done` such a tail is
the assistant's own prose and cutting it is permanent loss — the same trade
`render_options_as_text` makes for the other non-streaming channels. `on_done` delivers the WHOLE answer before uploads and the card, so
neither interrupts a chunked reply.

## Webex settings API

- `GET /api/webex/config` — masked `bot_token_preview` + `bot_token_set`,
  `connected` (true only while the device WebSocket is connected + authorized
  this session), `connect_error`, `configured` (token AND enabled AND non-empty
  allowlist — the transport fails closed on an empty list), `read_only`
  (true unless the request is direct-local), plus `enabled`, `allowed_emails`,
  `allow_group_rooms`, `allowed_room_ids`, `reply_in_thread`,
  `soft_threshold_pct`, `hard_threshold_pct` and `session_folder`. Never returns a
  raw secret.
- `PUT /api/webex/config` — requires a direct-local request (loopback peer
  AND no forwarding headers); remote gets 403. Validate-first/commit-last.
  New tokens (an accidental `WEBEX_BOT_TOKEN=` env line is stripped) are
  verified against Webex `GET /v1/people/me` before storage; rejection
  returns 400 and writes nothing, network failure saves with
  `verify_warning`. `bot_token_clear` must be a strict boolean.
  `allowed_emails` accepts syntactically valid emails only.
  `allowed_room_ids` is de-duplicated with order preserved and is otherwise
  unvalidated: a Webex room id is an opaque base64 blob whose shape is the
  platform's to define, and a format guess would reject a legitimate id from a
  cluster this code has never seen. The two thresholds are clamped as a PAIR
  through the same helper the config dataclass uses, so a soft value above the
  hard one cannot make the soft nudge unreachable. Secrets land in
  `config_dir/.env` (atomic 0600) with `os.environ` synced; non-secrets go
  to `config.json` under `webex`, and any token set/clear purges the legacy
  `webex.bot_token` config fallback (config.json commits before .env so a
  crash between the two cannot resurrect the plaintext copy). Writes are
  serialized under the repo-wide config lock. All fields are boot-read, so
  `restart_required` is true on any actual change.

## WeCom channel

**Transport (`kiro_crew/wecom/`).** A concrete `MessagingTransport` over WeCom's
AI-bot **long connection** (`client.py`): one outbound WebSocket to
`wss://openws.work.weixin.qq.com`, authorized with a bot id + secret via an
`aibot_subscribe` frame. Inbound user messages arrive as `aibot_msg_callback`
frames carrying a server-assigned `headers.req_id`; the bot replies by echoing
that `req_id` back on `aibot_respond_msg` frames whose `body.stream.content` is
the **full accumulated text**, so each frame REPLACES the bubble rather than
appending to it. A `stream.id` groups the frames of one bubble and `finish=true`
seals it. Each inbound frame is fast-acked into its own turn task so the single
socket keeps serving pongs during a long streaming turn. Turns ride the shared
`TurnDriver` through `drive_turn`, so WeCom inherits redaction, the approval
ladder, `SilentRenderer` muting and `publish_turn_identity` rather than
re-deriving them.

**Reply-ACK semantics are the load-bearing detail.** `send_stream` returning True
means the frame reached the socket, never that WeCom accepted it. Acceptance
arrives later, on a **cmd-less** frame carrying `headers.req_id` plus an
`errcode` — the same envelope shape as a pong ack and as the subscribe ack, so
the three are told apart by id, not by shape:

- The **ping** ids are tracked in a set as they are sent.
- The **subscribe** id carries a fixed prefix (`_SUBSCRIBE_REQ_PREFIX`), which is
  what makes a rejected credential detectable at all. Without it the only signal
  is the server closing the connection immediately, which is also what an
  anti-kick looks like — so an operator with a wrong secret was pointed at the
  wrong cause. A rejected subscribe reports not-healthy on the settings badge
  (the documented compensating control for skipping save-time verification) and
  does **not** stop reconnecting, because a secret can be corrected while the
  gateway runs.
- Anything else is a **reply** ack. `846605` (unroutable `req_id`) and `846608`
  (bubble past the platform's 10-minute stream lifetime) are terminal for that
  `stream_id`, so the client records it and `stream_is_dead()` reports it.

Neither the `errcode` nor the `errmsg` is ever logged or put on the badge:
`errmsg` can echo the rejected payload. Only the classification is surfaced.

**With no usable stream the head goes out CONFIRMED, and it gates the tail.** That
fallback used `send_reply`, whose one-shot POST returns `None` — a lost head was
indistinguishable from a delivered one, and the tail went out regardless, leaving a
fragment the reader cannot tell is incomplete. A push is not a duplicate on this
path (no bubble is showing the text), so it is preferred, `send_reply` remains the
last resort when there is no warm conversation, and if neither delivers the head the
tail is WITHHELD and logged rather than sent alone.

**A withheld tail is the rule on BOTH head paths.** `close()` recovers a refused
head as a confirmed push and then releases the tail — and that recovery can itself be
refused, or find no conversation to push into. `_recover_unconfirmed_seal` therefore
REPORTS whether the head reached the reader, and `_release_pending_overflow` withholds
and logs when it did not. Without that the recovery path shipped a tail whose head
never landed, which is the same fragment the no-stream fallback already refuses to
send.

**A failed dispatch releases its dedupe entry.** The `msgid` window means "already
delivered", and a turn that RAISED was not. `WeComTransport.receive` calls
`forget_msgid` before re-raising, so WeCom's own redelivery — the mechanism that
exists to recover exactly this — is not suppressed as a duplicate of a message nobody
ever answered. Idempotency still holds for the success path, which is what the window
is for.

**Everything that awaits AFTER ingest sits inside the cleanup block.** The dispatcher
deletes the decrypted attachments in a `finally`, so an await outside it is a path on
which a gateway shutdown strands plaintext. The origin bind is one such await (it
offloads to a thread), and it now runs inside the `try` — before the turn, so a
mirrored reply still has its binding, but no longer outside the guard.

**The tail is HELD until the head is resolved.** An over-cap answer seals its head
into the live bubble and pushes the remainder, and the head's verdict arrives after
both — so sending the tail immediately put it ahead of a head recovered at
`close()`, and the reader met the answer's middle before its beginning with nothing
saying the order was scrambled. `on_done` therefore parks the tail on the renderer
and `close()` releases it AFTER the head recovery. The cost is that the tail lands
after persistence rather than before it, so a crash in that one-local-write-wide
window leaves history holding an answer the reader only partly received; that is the
lesser evil, because the misordering needed only a refused frame while this needs a
crash. The alternative considered — re-sending head AND tail on recovery — keeps the
old timing but duplicates every tail chunk.

**The sealing frame gets a SECOND look, for free.** A stream frame's ACK cannot be
waited on (every frame of a turn replays the one inbound `req_id`, so a waiter for
one can be resolved by another's), and `_send_final_chunk`'s dead-bubble check runs
microseconds after the send — so a refusal is normally invisible there and the
answer was reported delivered. `close()` asks again: `drive_turn` calls it in its
`finally`, after persistence and the post-turn notice, so the verdict has had the
length of that real work to arrive at no added latency. If the bubble is refused by
then, the head is re-delivered as a CONFIRMED push (its own `req_id`, so acceptance
is correlatable), consumed once so a second teardown cannot post twice. A bounded
sleep-and-poll was the alternative and is worse: fixed latency on every turn, and
it cannot exit early on success because "the ACK said 0" and "no ACK yet" are
indistinguishable — WeCom is not documented to acknowledge an accepted frame at
all. Delivering the head through the push path unconditionally is worse still: it
posts every answer twice and spends double the conversation's 30/minute budget.

**A sealed bubble rolls, it does not swallow the answer.** An agentic turn doing
tool work routinely runs past ten minutes, and the refusal is asynchronous, so
without this the renderer keeps "succeeding" into a sealed bubble and the rest of
the answer — including the final frame — is never seen. `renderer.py` therefore
consults `stream_is_dead()` before each frame and at `on_done`, and on a seal
mints a fresh `stream_id` and continues there. Because content is cumulative, the
continuation sends only the part the earlier bubble does not already carry.
Where it resumes from is deliberately conservative: the refusal is observed on a
LATER push, so the most recent frame is exactly the one that may have been
rejected, and the continuation resumes from the frame **before** it. One frame's
worth of text can therefore appear twice; a visible repeat is a better failure
than a silent hole. `on_done` rolls only when something is left to deliver, so a
sealed bubble that already holds the whole answer is left unsealed rather than
followed by an empty bubble.

An aged roll resumes EXACTLY, a sealed one conservatively, and the difference is
evidence rather than assumption. Every frame carries the bubble's full accumulated
text, so a non-terminal ACK rejection is normally superseded by the next frame in
the same bubble — unless no next frame comes because the bubble rotates for age,
in which case its last frame was never delivered and resuming after it would skip
it silently (`send_stream` returned True, so nothing reports the gap).
`WeComClient.stream_had_rejection()` answers the narrower question "was everything
written here accepted", covering terminal and non-terminal refusals alike, and the
aged path takes the conservative resume only when it says no. It means "the LATEST
verdict is a refusal", not "was ever refused", and both retirements are load-bearing
for different orderings: sending the next frame retires it (that frame carries the
refused one's text, and this is the only route on a deployment that never ACKs an
accepted frame), and an errcode-0 ACK retires it (the only route when an earlier
frame's refusal arrives AFTER the seal, since nothing is sent after a seal). Left
sticky, `_recover_unconfirmed_seal` re-pushed answers the platform had accepted —
the reader gets the whole thing twice. A TERMINAL refusal is never retired:
`_dead_streams` is separate and permanent, because that bubble can never be written
again whatever is sent to it. Making age
unconditionally conservative instead would repeat a frame's worth of text on every
turn past the rotation age.

Both offsets are REBASED onto the new bubble's start when it opens. They describe
deliveries the abandoned bubble accepted, which say nothing about its replacement,
and 846605 (unroutable `req_id`) refuses every replacement too — so carrying them
forward made a SECOND roll resume from a position recorded against the FIRST
bubble, past a span the replacement never delivered. Rebased, a bubble refused
before it accepted anything resumes exactly where it began, keeping the worst case
a visible repeat rather than a silent hole.

**Security model.** `authorize` is deny-by-default and owner-only: an empty
allow-list authorizes nobody, and the only route to "everybody" is the explicit
`wecom.allow_all_users` opt-in, which still denies a frame with no userid. Two
further gates run in `receive`, both before a turn is dispatched:

- **1:1 chats only.** Group traffic (`chattype` other than `single`) is refused
  and SEL-audited (`denied_group_chat`). A WeCom group is a shared disclosure
  boundary, and sessions are keyed on `userid` alone, so a group message would
  ALSO run inside that user's private DM session — publishing its history and
  tool output into the room, and letting the room steer a session the user
  believes is private. The userid allow-list does not help: the sender is
  allow-listed, the audience is not. Same reasoning as iMessage's group
  fail-closed. Per-group sessions plus a group allow-list are the prerequisite for
  lifting this, and Webex is the channel that has both: it keys a space session on
  `space:{room_id}` and admits one only when `webex.allow_group_rooms` is on AND
  the space is in `webex.allowed_room_ids`. WeCom is not there because neither
  half exists for it.
- **Redelivery suppression.** WeCom names `msgid` as the dedupe key and documents
  that a callback may be repeated, and each repeat would run the whole turn
  again — a second provider round-trip, a second set of tool side effects, two
  answers in the bubble. A bounded, TTL'd window suppresses the repeat. It is
  consulted AFTER authorization, so unauthorized traffic cannot evict genuine
  entries and reopen the gap; an absent `msgid` is never suppressed, because no
  id is no evidence of a duplicate.

`WECOM_SECRET` / `WECOM_BOT_ID` are on the sandbox agent env denylist, and
`pod/runtime.py` forces the channel off in a sanitized pod seed.

**Tool approval rides the ONE shared grant, with no WeCom command.** WeCom renders
no approve/deny widget (`max_buttons=0`), so `decider` is `None` and
`APPROVAL_INTERACTIVE` is deny-by-default: without an out-of-band grant every tool
request is refused with nothing the user could click. `ChannelTurn.auto_approve_session`
supplies `() -> safety_override().is_active()` — the same process-global grant the
dashboard toggle drives, read per request rather than captured at boot, so arming it
or letting it lapse takes effect on the next tool instead of after a restart. It does
not weaken the gate: the keystone, the governance ceiling and the deny-list all run
ahead of that rung in `TurnDriver`, so a hard DENY still wins and only the prompt is
skipped.

There is deliberately **no `/yolo` command here**, which is why the surface stays a
predicate rather than a handler. One switch with one answer beats a per-channel copy:
a channel-local command would need its own owner rule, and the single global
`KIROCREW_OWNER_ID` is compared against each channel's own id space, so on a host
running several channels the configured value belongs to at most one of them. The
operator arms the grant from the dashboard (or Slack) where that ambiguity does not
arise.

**Cancellation cleanup is offloaded, and submitting it is what makes it durable.**
Both `except BaseException` guards (the shared per-attachment ingest loop and
WeCom's transcribe step) delete through `cleanup_offloaded`, not `cleanup`:
`os.unlink` is a blocking syscall, TMPDIR is not always local, and the cap is ten
attachments per message, so inline it stalls the gateway on exactly the path a
shutdown takes. The two ways the offload can fail need OPPOSITE handling, and
getting it backwards is invisible either way. `CancelledError` from the await is
NOT a fallback trigger: `to_thread` submits to the executor before it awaits, so
by then the worker owns the deletion (and still drains through
`shutdown_default_executor`) — deleting again would repeat the work and put the
blocking syscalls back on the loop, on the very path where the stall is worst.
`RuntimeError` is the only fallback: the loop refused the work outright (closed,
executor shut down), so nothing else will ever delete those files. Both return
normally rather than re-raising, because the caller is about to re-raise the
exception it was handling and this one would replace the real reason the turn
ended.

**An attachment makes the message CONTENT, never a command.** Every command branch
returns before `_ingest_media` runs and a WeCom media URL lives ~5 minutes, so a
photo captioned `/new` reset the conversation and the picture never arrived — no
error, nothing said about it, and the URL was dead before the user could resend.
The rule is about the early RETURN, not about parsing: the command intercepts and
the bare-override usage reply are disabled when `inbound.attachments` is non-empty,
while `parse_mid_turn_override` stays live because it only strips a prefix and the
media still reaches ingestion on the same path. Slack draws the same line with `and
not files` on its stop keyword. A command sent in its own message is unaffected.

**`ChannelTurn.auto_approve_session` is what makes that promise true.**
`TurnDriver` has always accepted the predicate, but the shared `drive_turn` did not
forward it, so a channel driven through this skeleton could report the grant as ON
while every tool request still hit the deny-by-default path — invisible from the
channel side, since the `ChannelTurn` looked correct. The field is optional and
defaults to `None`, so every channel that offers no in-channel toggle is unchanged
and the grant is simply not consulted. It is forwarded as the CALLABLE: a grant
that lapses mid-turn stops auto-approving the rest of that turn.

**Table rendering converts a SLICE, never the whole answer.** `_carried` /
`_sent_abs` are offsets into `text()`, and a table conversion changes the string's
length — so converting the whole answer and slicing the result would index a
different string and every bubble rotation would drop or repeat text. `_render_slice`
therefore runs on the slice about to be sent, progress is recorded from the RAW slice
BEFORE the transform, and the final chunks are converted once after the split so the
sealing frame, the overflow pushes and the late head recovery all carry the identical
string. Identity today (`table_mode="off"`), which is why the ordering is pinned by a
test that fakes a length-changing transform: the two orderings are indistinguishable
until the policy changes, and then the failure is silent.

**Reply length is denominated in BYTES.** `stream.content` and
`markdown.content` are capped at 20480 UTF-8 bytes, so the transport declares
`max_message_chars = WECOM_MAX_REPLY_BYTES // 4` (`WECOM_SAFE_REPLY_CHARS`) and
`truncate_utf8` is the exact guard at the wire — the same derivation, and the same
reason, as Webex. Declaring characters directly is what let a Chinese reply sit
under the cap and land ~3x over it, where WeCom rejects the whole frame and the
user gets nothing. An answer longer than one bubble is DELIVERED rather than
truncated: `on_done` splits the remainder with `split_markdown_safe` (fence-safe,
because WeCom renders markdown), seals the FIRST chunk on the live bubble the user
is already watching, and delivers the tail as **proactive pushes** — because a
stream frame's acceptance cannot be confirmed (every frame of a turn replays the
one inbound `req_id`, which is the only key an ACK carries, so a waiter for one
frame can be resolved by another's ACK), while `aibot_send_msg` mints its own
unique `req_id` and its verdict is exact. A refused tail chunk is therefore
reported with its position rather than assumed delivered. This matters because
`drive_turn` persists the full text, so a silent truncation would leave history and
delivery disagreeing about what the user was told.

**Reasoning is redacted on the JOINED text, because the join is the risk.**
`TurnDriver` redacts each thinking chunk, but with a plain per-chunk pass rather than
the rolling `StreamRedactor` it uses for the answer — so a credential split across
two chunks passes both halves and is reconstituted by the renderer's join. The
assembled string is redacted at the send boundary, which closes that and also covers
an unsplit credential. Same placement, and the same reason, as Slack's
`_maybe_post_thinking`.

**Reasoning has a native home.** WeCom renders whatever sits inside
`<think></think>` in `stream.content` as a collapsed reasoning block, so
`on_thinking` streams there instead of dropping reasoning (as it did) or faking it
as body text — no sibling channel has this affordance. The turn-start placeholder
uses the same wrapper, so it is not stranded above the answer.

**`[OPTIONS:]` degrades to a numbered list** through the shared `format_overflow`
sink, which owns the numbering plus the display-form credential redaction and
mention defanging — a choice is LLM-authored text landing in a message body.
Deleting the trailer, as this did, meant the user never learned the choices
existed. `max_buttons` stays `0` because no tappable widget is rendered. WeCom
keeps its own `_render_options_as_text` rather than the shared
`render_options_as_text` for one reason, and the contract test names it: WeCom
STREAMS, so an unterminated `[OPTIONS…` tail there really may be a marker
mid-flight and is hidden, where the buffer-and-send-once channels keep it as prose.
That difference is now expressed as `split_options_trailer(..., hide_partial=True)`
rather than as a second copy of the parse — only the folding into `format_overflow`
stays local.

### One parse of the `[OPTIONS:]` marker

`split_options_trailer(text, *, hide_partial=False) -> (body, choices)` is the
single parse. `render_options_as_text` returns only the body, so every
widget-capable renderer used to carry its own copy — six of them, three (Discord,
Telegram, Teams) identical down to the comment. A parse duplicated per channel
drifts per channel and does it silently, because each copy reads correctly alone.

`hide_partial` is the one thing the channels genuinely disagree about, so it is a
parameter rather than a baked-in policy:

- **`True` — a streaming surface** (Discord, Telegram, Teams, WeCom, and Webex's
  status frame). Text is still arriving, so an unfinished `[OPTIONS` fragment may
  be a marker mid-flight; hiding it keeps reserved protocol off the screen, and the
  next frame re-renders from the full buffer, so nothing is lost.
- **`False` — a buffered surface that sends once** (Webex's final answer and the
  zero-widget path). Such a caller cannot tell a live fragment from prose, and
  cutting prose is permanent: a reply ending `see the [OPTIONS section` keeps its
  last four words.

The default is `False` because the failure directions are asymmetric — a needless
keep flashes markup for one frame, a needless cut deletes unrecoverable text — so a
caller that forgets degrades toward the cosmetic failure, and every streaming caller
states `True` explicitly, which also makes the destructive choice greppable.

**`slack/format.py::extract_options` is deliberately NOT converged.** It parses the
LINE grammar (`OPTIONS_RE_LINE` — `re.MULTILINE`, end-of-*line*), not the
end-of-buffer `OPTIONS_RE_TRAILER` every other channel uses, so routing it through
this helper would silently stop matching a marker that ends a line mid-message.
Different grammar, not a duplicate. `test_options_cap_contract.py::TestOnlyOneTrailerParseExists`
greps the tree for a re-derived parse and records both exemptions.

**Proactive push works, per-target.** `aibot_send_msg` needs no token and has no
expiry, but WeCom only delivers into a conversation the user has already written
to. So `supports_proactive_send` is `True` (the transport CAN push) while
availability is answered per target by `configured_targets()`: an allow-listed
userid that has never messaged the bot is listed with a reason rather than offered
and then failing at send time. Warmth is learned from AUTHORIZED inbound only, so
an unauthorized sender cannot make itself a mirror target.
`resolve_configured_target()` rechecks MEMBERSHIP at the side-effect boundary and
deliberately does not recheck warmth: `_warm_chats` is in-memory while a mirror
binding is persisted, so after a gateway restart warmth is UNKNOWN rather than
known-false, and refusing on it would silently disable every mirrored send until
the user happened to write again. Deliverability is WeCom's answer to give — the
push is attempted, the refusal arrives on the ACK that `send_proactive` waits for,
and `send_message` raises `WeComSendError`. Warmth stays where being wrong is
cheap: the availability hint in `configured_targets`. This is what makes `/link`, the dashboard mirror and cron
delivery reachable at all; the origin mirror is bound on every turn and withdrawn
only by `/unlink`, matching Telegram and Discord.

**Every out-of-band ack is a PUSH, and its one-shot fallback reports a verdict.**
`client.say` carries every command ack, notice and refusal. As a bare stream frame
its refusal was recorded against the stream and nothing acted on it — the user
pressed a command and got nothing back, which reads as the command not existing
rather than as a delivery failure. It now prefers `aibot_send_msg` (own `req_id`, so
acceptance is confirmed; the conversation is warm by construction because this only
runs on a turn the user just sent), falls back to a sealed stream frame for a
deployment where the push is unavailable, then to `response_url`.

`send_reply` returns whether the platform ACCEPTED it. The status and errcode were
always inspected and then discarded, which forced callers to guess — the renderer
treated "a `response_url` exists" as proof of delivery, so a failed POST looked
identical to a delivered one and a headless answer tail went out behind it. Its
refusal log carries the http status and a classification only: `errmsg` can echo the
rejected payload, which is the answer text.

**The empty/error seal is recovered like any other.** That branch carries the failure
notice, and `drive_turn` swallows a provider error — so if the frame is refused and
nothing recovers it, the user is told nothing at all about a turn that failed while
the inbound `msgid` stays deduped against WeCom's retry. Routing it through
`_send_final_chunk` is what keeps "the turn was answered" true, which is the premise
the dedupe window's correctness rests on.

**The threshold notice is a PUSH, not a stream frame.** It is sent post-turn,
between `on_done` and the renderer's `close()`, and `client.say` opens a fresh
stream on the SAME inbound `req_id` — the only key a cmd-less ACK carries, so the
client attributes an arriving ACK to the newest stream sent on that req_id. Sending
the notice there retargeted that attribution: a refusal ACK for the ANSWER's
sealing frame landed against the notice, leaving the answer looking accepted and
defeating the `close()` recovery in exactly the case it exists for. `send_proactive`
mints its own req_id, so it cannot collide, and its acceptance is confirmed rather
than assumed — the better guarantee for a message the user must actually see. The
conversation is warm by construction: this runs on a turn the user just sent.

**The per-turn origin bind is offloaded too.** `bind_origin_mirror` consults
`SessionManager.mirror_opt_out`, and that read WRITES — a refusal stored under an
older generation key is promoted to the bucket inside `batched_save`, whose block
exit rewrites the whole map inline on the calling thread. That runs on EVERY inbound
message, so on the loop a one-time migration stalls every other conversation and the
WS heartbeat behind a disk write. `set_origin_link` beside it deliberately stays on
the loop: it reaches `_save` unbatched, whose loop-aware branch schedules one
debounced flush that writes in a worker thread anyway.

**The link commands persist OFF the loop.** `SessionManager.set_mirror_opt_out`
opens `batched_save` internally (two flag writes, atomically) and
`release_conversation_location` opens one of its own (three clears, so the location
is never half-freed while the reply claims ✅). A batch block's exit writes the
whole map INLINE on the thread leaving the block, so on the event loop it is a
synchronous disk write on a map that grows with every session ever created —
stalling every gateway turn and the WS heartbeat. Both calls therefore go through
`asyncio.to_thread`, awaited in sequence so the refusal still lands before the
release. The unbatched mutations beside them (`set_mirror_link`,
`clear_mirror_link`, `set_origin_link`) stay on the loop deliberately:
`SessionMap._save`'s loop-aware branch marks the map dirty and schedules one
debounced flush that writes in a worker thread, and wrapping those in a batch would
reintroduce the inline write. Pinned by thread identity in
`test_wecom_commands_and_ingest.py`, because what makes the write safe is not being
on the loop.

**Inbound media is an encrypted CDN fetch with a PER-OBJECT key.** An
`aibot_msg_callback` never carries bytes: `image` / `file` / `video` carry a
~5-minute `url` plus their own `aeskey`, and the object is **AES-256-CBC**,
PKCS#7-padded to a 32-byte multiple, IV = the key's first 16 bytes. `wecom/media.py`
owns that protocol work and `wecom/attachments.py` maps each item onto the shared
`messaging/attachments.py` pipeline, so classification, limits, signature
validation and temp-file ownership stay channel-neutral. This is deliberately NOT
Weixin's scheme (AES-128-**ECB**, shared key) and the two must not be merged: the
mode, key length and key scope all differ. The `aeskey` arrives in two encodings
for the same value (base64 of raw bytes, base64 of ASCII hex), discriminated by
decoded length plus a strict hex check, because guessing wrong yields plausible
garbage rather than an error. The download cap is enforced on BYTES READ, never on
`Content-Length` — and it is the plaintext ceiling **plus the padding**, because
what is read is ciphertext: PKCS#7 to a 32-byte multiple always adds 1–32 bytes, so
a file at exactly WeCom's 20 MB maximum arrives larger than it is and a cap set to
the plaintext figure refused precisely the largest valid attachments, before
decryption. `WECOM_MAX_PLAINTEXT_BYTES` is exported from `wecom/media.py` and the
ingest limits take it from there, so the two cannot drift. `voice` is excluded from the download path on purpose: WeCom
returns its OWN transcript in `voice.content`, so the text is the payload and no
shipped backend decodes the codec — which is also why `WECOM_INGEST_LIMITS`
budgets audio at the 20 MB **file** ceiling and not at WeCom's 2 MB voice-message
limit: no voice bytes ever reach the ingest path, so the only audio that does is a
file the user attached, and the voice limit would have refused a 5 MB `.mp3` that
WeCom itself carried. `max_text_bytes` is the one cap left at the shared default
on purpose — it budgets bytes READ into gateway memory, of which only
`max_text_inject` can reach the prompt, so it is not a transport ceiling and must
not be raised to one. A `mixed` message's caption lives in its item
list rather than in `text`, and is read from there — and a media-only message is a
message, so the empty-text early return now also requires no attachments.

**Deliberately not implemented here**, and each a platform capability we do not
use yet rather than a limit: interactive `template_card` buttons and their
`template_card_event` callbacks (which is why `max_buttons` stays `0` — doc
`/101032` says the interactive card types require a configured callback URL, which
is in tension with long-connection mode, and declaring a widget capability that
cannot be verified against a live bot is the exact dishonesty
`test_capability_ledger.py` exists to prevent); outbound media upload (the 3-step
chunked `aibot_upload_media_*` sequence, which needs request/response correlation
the client does not yet have, so `files_outbound` stays `False` and an image
reference keeps printing its path — the honest degradation); per-group sessions;
and the `enter_chat` / `feedback_event` events. `_handle_event` recognizes those
event types and drops them deliberately: each owes a reply inside a 5-second
single-delivery window, so answering one is a feature with its own design.

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

## Microsoft Teams channel

**Transport (`kiro_crew/teams/`).** A concrete `MessagingTransport` over a
pure-`aiohttp` Bot Framework client (`client.py`) plus `PyJWT` for inbound token
validation — no Bot Framework SDK dependency (the optional `kirocrew[teams]`
extra). Teams is the **only** channel whose inbound is a public HTTPS endpoint:
every other channel opens an outbound connection, but "Teams sends a JSON object
to your agent's messaging endpoint, and it allows only one endpoint for
messaging." There is no Socket Mode equivalent, so this is a permanent
architectural divergence, not a gap to close.

**Inbound authenticity is the channel's whole trust boundary.** `on_activity`
extracts the `Authorization: Bearer` token and runs `JwtValidator.verify` off-loop
(`asyncio.to_thread` — a JWKS fetch and an RS256 verify must never sit on the
gateway loop) BEFORE the activity is ACTED ON, returning 401 with a SEL
`denied_invalid_token` row. The route reads and JSON-parses the body first, under
`TEAMS_MAX_ACTIVITY_BYTES`, because that is what bounds it; the guarantee is
"nothing is dispatched pre-auth", not "nothing is read pre-auth". `verify` pins `algorithms=["RS256"]`, sets
`audience` to the bot's App ID, requires `exp`/`iss`/`aud`, allows the
documented five-minute clock skew, and rejects any issuer outside
`{"https://api.botframework.com"}`. `_require_https` pins the scheme of both the
OpenID metadata URL and the resolved `jwks_uri`, closing an arbitrary-file-read
vector (`PyJWKClient` would honour `file://`). `_dispatch_activity` then binds the
outbound target to the token's own `serviceurl` claim: the reply carries an
app-credential bearer token, so a replayed activity pointing `serviceUrl` at an
attacker-controlled host must not receive it.

`_dispatch_activity` also binds the channel POSITIVELY: `activity.channelId` must
equal `msteams`. An Azure Bot resource has Web Chat enabled by default and can
carry Direct Line, and both reach this endpoint with a token that passes every
check above — same issuer, same audience, matching `serviceurl` claim — while
defaulting `conversationType` to `personal`. On Direct Line the CLIENT composes the
`from` object, so `aadObjectId` is not channel-attested and `teams.allowed_emails`
would be matching an identity the sender chose. A negative test ("not some other
channel") would fail open on the next channel Microsoft adds.

Five bounds sit around that check, because this is the one route reachable from
the internet:

- **Route reachability.** `csrf_middleware` applies an Origin check to every
  non-safe method, and the Connector sends neither `Origin` nor `Referer`, so the
  request is refused before the JWT handler runs unless the peer happens to be
  loopback. The route is therefore in the **method-scoped CSRF exemption for
  self-authenticating external webhooks** — sound because CSRF defends against a
  browser attaching cookies cross-origin, and this handler ignores cookies and
  authenticates a JWT a browser cannot forge. Only `POST` is exempt; `PUT`/`DELETE`
  on the same literal path still match dashboard-authed wildcard routes.
- **Failed-auth throttle**, reusing `webhooks.py`'s existing counters, so an
  anonymous flood cannot spend one SEL row per request. Note its SCOPE: the
  throttle is skipped for a proxied request (`is_proxied_request`) because it would
  otherwise key every caller onto the proxy, and two of the three documented
  topologies are proxied. So it is NOT what bounds the JWKS refetch.
- **JWKS refetch damper.** `PyJWKClient.get_signing_key` answers an unknown `kid`
  with an unconditional `refresh=True` fetch and has no rate limit of its own, so
  each bogus-kid POST would buy one outbound HTTPS GET. `JwtValidator._get_signing_key`
  therefore does the kid lookup itself — cached set first, then at most one
  refetch per `_JWKS_REFRESH_MIN_INTERVAL_SECS` — so the damper sits next to the
  fetch it bounds rather than at a route that may not run. A genuinely rotated key
  is still reachable within one interval.
- **Bounded body.** The dashboard route reads the activity under
  `TEAMS_MAX_ACTIVITY_BYTES` and stashes the parsed dict under
  `TEAMS_ACTIVITY_REQUEST_KEY`, so `on_activity` never re-parses an unbounded
  body. The cap lives in the route, keeping `client.py` free of dashboard imports.
- **Replay drop and an in-flight ceiling.** The Connector legitimately redelivers
  when the bot misses its ack window, so a duplicate `activity.id` is dropped
  idempotently (audited `denied_replayed_activity`) rather than refused — checked
  AFTER attestation so an unattested activity cannot consume a dedupe slot. A
  valid-token burst is shed past `_MAX_INFLIGHT_TURNS`, since each turn holds a
  session semaphore and a provider process. Two shapes are EXEMPT, and must be,
  because both are how a saturated gateway gets UNstuck: a card click (Teams
  delivers `Action.Submit` as an ordinary `message` activity, so shedding it drops
  the Approve/Deny press that would free a slot and deadlocks every waiting prompt)
  and `/stop`, whose aliases are DERIVED from `COMMAND_SPEC` rather than copied.
  Neither starts a turn, so neither costs a semaphore.

The handler fast-acks 200 and runs the turn in a background task: the Connector
times out the inbound POST at ~15 seconds, far below an agentic turn.

**Outbound.** `POST/PUT {serviceUrl}/v3/conversations/{id}/activities[/{activityId}]`
with a cached client-credentials token (`login.microsoftonline.com`, scope
`https://api.botframework.com/.default`; the tenant is templated so single-tenant
works). Delivery failure **raises** `TeamsSendError` rather than returning `None`:
every caller treats a return as proof of delivery, so a swallowed error made the
gateway record an answer the user never received. Callers that legitimately
tolerate failure — the typing indicator, a cosmetic in-place edit, a command
acknowledgement — catch it at their own call site. Retries cover Teams' documented
transient set, which is **wider than the usual 429-only rule**: `412`, `429`, `502`
and `504`, honouring `Retry-After` and otherwise backing off exponentially. The
status badge is bidirectional — a delivered activity clears a stale failure, and
`_notify_state` dedupes on the transition so a healthy channel does not republish
per send nor overwrite the first failure reason.

**serviceUrl durability (`service_urls.py`).** The Bot Framework offers no way to
look up where a conversation can be reached: `serviceUrl` arrives on an inbound
activity and the bot must remember it. An in-memory map lost every proactive
destination on restart, so a cron result or dashboard mirror leg had nowhere to
send until the user spoke again. `ServiceUrlStore` persists
`conversation_id -> serviceUrl` (plus the allow-listed identity that owns each
conversation, recorded only AFTER authorization) to
`$KIROCREW_HOME/routing/teams_service_urls.json`. Loading is lazy and off-loop —
never on the gateway boot path — every failure degrades to the in-memory map because
a lost routing hint must not stop delivery, a non-Connector row does not survive a
reload, and the map is bounded by count with least-recently-seen eviction.

It lives in its own `routing/` directory because that directory is a keystone leaf,
so the agent can neither read nor write it: the identity map is what an explicit
`user:<upn>` send target resolves through, and a writable copy delivers one person's
cron result to another. The DIRECTORY is registered rather than the file, so
`atomic_write`'s `mkstemp` temp sibling is covered too — see
[security](security.md#crew-data-home-secrets--governance-trust-root) for why that
distinction is load-bearing. There is no migration from the pre-`routing/` path:
reading the old, agent-writable location would reopen the hole.

Two paths besides an ordinary message keep that map honest, one per direction:

- **Learning without a prompt.** A personal-scope `conversationUpdate`
  (membersAdded) or an `installationUpdate` carries the whole routable tuple —
  conversation id, `serviceUrl`, `aadObjectId` — under exactly the attestation
  above and no prompt, so `TeamsClient.on_route` hands it to
  `TeamsTransport.note_route`, which re-applies the SAME personal-scope and
  allow-list gates `receive` does. A freshly installed app is therefore a proactive
  target before the user first types, without a Connector conversation-creation
  round trip; a join from a channel or a stranger records nothing, so "reachable"
  never comes to mean something other than "authorized".
- **Forgetting a dead route.** Capacity eviction is not enough: once a user blocks
  the bot or removes the app the route is permanently undeliverable, and keeping it
  turns every later cron result and mirror leg into a red badge with nothing able to
  clear it. `TeamsSendError` carries the Connector's `status`, and
  `TeamsTransport.send_message` calls `ServiceUrlStore.forget` on a PERMANENT
  refusal only (`403`/`404` — not `401`, our credential, and not `429`/`5xx`, which
  are transient), dropping the identity row with the conversation and persisting it
  so the next process does not re-advertise it.

**Security model.** `authorize` is deny-by-default against
`teams.allowed_emails` (matching the UPN/email when Teams supplies one, else the
AAD object id, since activities carry that more reliably); an empty allow-list
authorizes nobody. **Personal-scope only, fail closed:** any non-`personal`
conversation type is denied and audited BEFORE authorization, because a reply in a
channel or group chat would expose tool output to members who are not on the
allow-list. `MICROSOFT_APP_ID` / `MICROSOFT_APP_PASSWORD` /
`MICROSOFT_APP_TENANT_ID` are on the sandbox agent env denylist, and
`pod/runtime.py` forces `teams.enabled = false` in a sanitized seed and scrubs the
`MICROSOFT_APP_` prefix — a pod that inherited a real config would otherwise drive
the operator's production bot and answer real people.

**Dispatch + rendering.** Turns ride the shared `TurnDriver` through
`messaging/dispatch.py::drive_turn`. The command vocabulary lives in ONE table,
`teams/commands.py::COMMAND_SPEC`, which drives both the parser and the `/help`
card so the two cannot drift: `/new`, `/compact`, `/stop` (alias `/cancel`),
`/yolo`, `/link`, `/unlink`, `/sessions`, `/dashboard`, `/help`, plus the `/queue` and `/steer` per-message
directives. A bare directive answers with usage rather than handing the
literal `/queue` to the model, which would reply to it as chat text.
`/dashboard [<N>h|<N>m]` MINTS a presigned dashboard login token for the asking
identity (default and cap from `commands.parse_dashboard_ttl`) and is SEL-audited,
so every address on `teams.allowed_emails` can issue itself a dashboard session —
the same premise that scopes `/yolo` to one conversation.

**`/sessions` continues a dashboard chat here** (`teams/session_resume.py`), on the
same shared core Discord uses. What Teams supplies is the widget — an Adaptive Card
whose `Action.Submit` returns as an ordinary message activity, so it needs no `invoke`
handler — plus its own display redaction and command spellings. Three properties are
Teams-specific and load-bearing:

- **Owner-only, and STRICTER than Discord's rule.** Discord requires exactly one
  configured user id. Teams' allow-list routinely holds several people, and a dashboard
  session is the operator's whole working transcript, so listing is refused unless
  `teams.allowed_emails` holds exactly one identity. With more than one it refuses
  everybody rather than picking the first entry — the same premise that scopes nothing
  else on that list to one person.
- **The submit carries an INDEX, never a session key.** A key in the payload would be an
  instruction to bind whatever the sender named; an index is resolved against the list
  this process actually offered, so a forged or replayed press can only ever miss. The
  registry additionally scopes on the owner and on the posting the press came from.
- **Routing runs BEFORE the command intercept.** `/compact` and `/stop` act on the
  RESOLVED session, so after a binding was destroyed they would compact or cancel the
  native Teams session while the user believes they drive the resumed one. Deciding once,
  upstream, makes that structural instead of something each handler must remember; the
  decision is then threaded into the turn and never re-resolved. `/sessions`, `/new`,
  `/unlink` and `/help` stay reachable while routing refuses, because a user whose link
  broke needs the way back in. A resumed turn does NOT rotate its generation — rotation
  belongs to this conversation, and applying it would move the user off the transcript
  they just attached to. `/new` and `/unlink` release the binding, and a release that
  cannot be made durable changes nothing and says so.
- **A refusal that did not land settles nothing.** Settlement clears (or adopts) the
  durable record the refusal was owed for, so applying it after an undelivered notice
  routes the user's NEXT message into a transcript they were never told about. Both
  channels gate on delivery: Discord on `send_message`'s own boolean, Teams on `_reply`'s
  (which is why that helper returns one at all — a cosmetic command ack is still logged
  and swallowed, but a notice that gates durable state is not cosmetic). An unsettled
  record owes the same refusal again, which is the direction that fails safe.
- **A card click resolves against BOTH keys of its conversation.** The turn registers
  its decider and its renderer under the key it ran with, which for a resumed
  conversation is the bound `dashboard:` one — so `_handle_card_action` keyed only off
  the native `teams:{email}` session would find neither, tell the user the prompt is
  stale, and let the tool deny by default at the prompt timeout. It tries
  `_click_session_keys` (resumed, then native) in order. Both, not just the resolved
  one, because a card click is a relief activity that bypasses the busy check: a
  `/sessions` pick can bind the conversation while an earlier turn is still in flight,
  and that turn's cards stay registered under the key it started with. Both keys belong
  to the same conversation and identity, so this widens nothing — at most one of them
  holds a given `(request_id, nonce)` pair, and the per-prompt nonce still decides.

**Which identity the allow-list authorizes.** A Teams activity may carry a UPN, an
AAD object id, or both, and `teams.allowed_emails` accepts either form. So
`TeamsTransport._resolve_identity` picks the form the list actually AUTHORIZES rather
than a fixed preference order: a plain email-first rule denies a user whose OBJECT ID
is allow-listed whenever Teams also sends an email — the ordinary shape for a guest
account and for any tenant that lists object ids — with the entry sitting right there
in the list. When both forms match, the email wins, so an install listing both keeps
the human-readable session key it already had. An unauthorized sender falls back to
email-then-object-id so the deny audit names them recognisably.

The decision is then CARRIED, on `TeamsInbound.resolved_identity`, and read in exactly
ONE place per module: `TeamsTransport._resolve_identity` makes it,
`TeamsDispatcher._identity` reads it. Every re-derivation is a chance to disagree, and
each one that existed did: the transport admitted a user on their object id while the
dispatcher keyed the session on their UPN (a session nobody authorized, which owner-only
`/sessions` then refuses), and `handle_message` keyed a turn on the UPN while
`_handle_card_action` keyed the click on the object id (so the approval card resolved
against a session nothing was awaiting, expired, and the tool denied by default — and
`/new` rotated a generation the turn was not using). `_identity` falls back to
email-then-object-id for an inbound built outside `receive` (tests, and the route-only
activities that never reach a turn), which is the same answer whenever only one form is
present.

**The credential check verifies outside the config lock and CONFIRMS inside it.** The
save-time Azure token exchange is a network round trip, and `_get_config_lock()`
serializes every config writer in the process — holding it across that call would stall
unrelated saves, and a hung endpoint would wedge them until the timeout. So the check runs
first, records the exact `(app_id, password, tenant)` Azure accepted, and the commit path
re-derives that triple under the lock and refuses (`config_changed`) if it moved. Without
the confirmation, two concurrent saves — one changing the app id, one the secret — each
verify a triple containing the other's old value, both pass, and the serialized commits
merge into a stored triple neither one checked: a green "Saved." and a channel that is
dead at the next restart. Optimistic concurrency, not a longer lock.

**A quote-reply is unwrapped before anything reads the text.** Right-click → Reply
in a 1:1 chat — the only scope this channel serves — makes Teams PREPEND the quoted
message to `activity.text`, while the user's own words sit in the `text/html` body
attachment after a `<blockquote itemtype="http://schema.skype.com/Reply">`.
`attachments.quoted_reply_text` recovers them, and the client prefers that over
`activity.text`. Without it a quote-replied `/stop` no longer starts with `/`, so it
reaches the model as prose and the turn keeps running, and a quote-replied question
arrives with the previous message jammed onto the front. The helper returns `""` for
any shape it does not recognise, so an unfamiliar client degrades to `activity.text`
rather than losing the message, and the body attachment is still never ingested as a
FILE (that would duplicate the prompt).

**Tool approval is an Adaptive Card** (`teams/cards.py`, `teams/approvals.py`).
Teams renders no Block Kit and no message components, but an `Action.Submit` on a
card returns as an ORDINARY `message` activity whose `value` carries the button's
payload — which is why `Action.Submit` is used and `Action.Execute` is not: a
universal action arrives as an `invoke` needing a synchronous
`{statusCode, type, value}` body, incompatible with the fast-ack-then-background
shape the Connector's inbound timeout forces. The card offers Approve / Trust
session / Deny, mirroring Slack's three, and `[OPTIONS:]` choices ride the same
mechanism as chips through the shared `apply_options_cap` (`max_buttons=5` — BELOW
Slack's 10, because Adaptive Card actions render as full-width buttons on mobile —
and overflow degrades to a numbered text list rather than vanishing).

**A chip is turn content, never a command.** The chip re-dispatch passes
`interpret_commands=False`, exactly as the queue drain does, and that is a security
boundary rather than a nicety: a label comes from the MODEL's own `[OPTIONS:]`
trailer and display redaction does not strip a leading `/`, so with interpretation
on a model that emitted `[OPTIONS: /dashboard | cancel]` would render a chip whose
single tap mints a dashboard login credential.

Two properties make a click safe, and both are the reason this is not a bare dict
of futures:

- **A stale card cannot answer a live prompt.** ACP request ids restart at 1 in
  every provider process, so a card still sitting in a chat from a previous run
  can name an id that is live again for a DIFFERENT tool. Every prompt mints a
  nonce, compared on resolve; the registry key is namespaced by session because
  two sessions can await id `1` at once. A timeout, an unknown id, a mismatched
  nonce and an already-answered prompt all resolve to "not approved".
- **The payload carries no authority.** A submit is client input, so it is only
  ever a LOOKUP key into state this process holds. An option chip's label is read
  from what that turn actually offered, never from the payload, so a forged label
  cannot be injected as if the user had typed it.

An answered prompt's card is REPLACED with its outcome (`resolved_card`, no
actions), so a chat never accumulates live buttons that resolve to nothing; a
click that resolves nothing tells the user, because a button that silently does
nothing is indistinguishable from a broken bot. Three cases reach that same
replacement, because "answered" is not the only way a prompt stops being live:

- **Expiry.** The decider calls `on_expired` (wired to the renderer, which is the
  only thing that knows where the card is) when the click window closes, so the
  buttons do not keep looking live forever.
- **A card that never landed.** The nonce is armed BEFORE the post so a fast click
  is not refused as stale, which means a failed post leaves an armed prompt with no
  control. `TeamsApprovalDecider.abandon` denies it AT ONCE and the renderer says
  so, instead of parking the turn for the full window behind a card nobody received.
  A delivered card whose activity id Teams merely WITHHELD is not this case; both
  read as an empty string, and `_card_posted` is what separates them.
- **A chip pick.** `settle_options` replaces the chips card with the choice before
  the turn runs, so no other chip still looks live and the transcript records which
  one was picked. If the chips card could not be posted at all the choices degrade
  to a numbered text list — the trailer was already cut from the body, so silence
  would lose them — and the nonce is dropped so no later press resolves against a
  card that does not exist.

**The typing indicator is kept alive.** A Teams typing activity expires after a few
seconds, so one at turn start leaves a minutes-long turn showing dots briefly and
then a silent chat, which a user cannot distinguish from a dead bot. The renderer
refreshes it every `_TYPING_REFRESH_S` until the turn finalizes (~1% of Teams'
7 requests/second per-thread budget); the progress bubble only covers a turn that
CALLS a tool, so a long pure generation or a `/compact` has nothing else to show.
Both `on_done` and `close` cancel the loop, because an orphaned refresh would post
into a finished conversation for the process lifetime.

`ChannelTurn.auto_approve_session` is the second rung, letting a user stop being
asked per tool. Teams passes `() -> safety_override().is_active()` — the ONE
process-wide grant, and nothing else. The field is an **additive** widening of the
shared pipeline (`None` default, and `TurnDriver` already accepted the parameter),
so a channel that does not set it keeps deny-by-default byte for byte.

Both ways a Teams user can arm that grant go through the SAME shared helper:
`/yolo` calls `messaging.commands.run_yolo_command`, and the card's middle button
calls it with `"on"`. So the duration, the expiry, the renewal grammar and the SEL
row are identical whichever way the user asked, and neither can disagree with the
dashboard toggle. Teams deliberately keeps **no grant store of its own** — a
channel-local trusted set would be a second grant with its own lifetime and its own
answer to "is YOLO on?", and it would have had to reimplement expiry, renewal and
auditing that the shared helper already owns. `approvals.TeamsApprovalDecider` only
RECORDS that the button was pressed (`trusted`), because arming is async and audited
while the click path is sync; the dispatcher arms it, before settling the card, so
the label cannot claim a grant that failed to arm. The grant does not weaken the
PreToolUse gate: the sensitive-path keystone, the governance ceiling and the
deny-list all run ahead of the auto-approve ladder, so a hard DENY still wins.

The renderer lazily opens ONE progress
message on the first tool call and edits it in place (throttled — Teams' limit is
7 requests/second per thread), and at `on_done` reuses that message for the first
chunk of the answer so no "Working…" bubble is stranded above the reply. Splitting
goes through the shared fence-safe `split_markdown_safe` off-loop, not blind
fixed-width slicing, so a long reply cannot be cut through the middle of a code
fence. The delivered text is re-scanned in its **display** form
(`messaging/display_safety.py::redact_for_display`) at that single chokepoint,
because stripping the trailer and letting Teams render markdown can reassemble a
credential the driver's scan saw as broken. Messages carry
`textFormat: "markdown"`; note Teams renders only a markdown SUBSET in a plain
message (bold, italic, preformatted, blockquote, links — **not** headings, lists,
tables or images), which is the largest remaining content-fidelity gap versus
Slack mrkdwn.

`on_thinking` is a no-op, matching every non-Slack channel.

### Teams' file halves (`teams/attachments.py`)

Both capability flags are `True`, and both are deliberately narrower than the
platform. `teams/attachments.py` owns only what is Teams-shaped; classification,
limits, signature validation, temp-file ownership, reference scanning, the
security floor and the byte budgets all stay in `messaging/attachments.py` and
`messaging/outbound_files.py`.

**Outbound: inline images only, and that is a scope decision, not a gap.** An
`Attachment` whose `contentUrl` is a `data:image/png;base64,…` URI renders with no
hosting and no round trip, which covers the case this exists for — an
agent-produced chart. A NON-image file would need the `FileConsentCard` flow:
a consent card (`application/vnd.microsoft.teams.card.file.consent`), a user
accept, a `fileConsent/invoke` activity carrying `uploadInfo.uploadUrl`, a `PUT`
of the bytes with `Content-Range`, then a `…card.file.info` confirmation — plus
`supportsFiles: true` in the app manifest. Be precise about the blocker: that
`invoke` needs no synchronous body of its own (unlike `Action.Execute`, which does —
see the card section), so the fast-ack ingress is not what rules it out. What rules
it out is SCOPE: five new wire shapes, a per-upload state machine keyed on a consent
the user may never give, and a chunked `PUT` — for a case an inline raster already
covers. A non-inlinable reference is refused visibly instead, and this paragraph is
the record of the decision rather than of an impossibility.

- **The seal is `on_done`, and it is the only one.** Teams does not stream, so
  there are no live frames that could flash markup before the seal replaces it and
  no length rotation that could bisect a reference — the two hazards Discord's
  `hide_local_refs` and upload-eligibility flag exist to handle. Extraction runs
  once per turn through `extract_local_refs_off_loop`, gated behind a `"!["`
  substring pre-check so an ordinary answer never touches the filesystem.
- **Named ceilings fed in as budgets.** `TEAMS_MAX_INLINE_IMAGE_BYTES` (1 MiB,
  Teams' documented picture limit), `TEAMS_MAX_INLINE_IMAGES` (4 — each image is
  its own activity against a 7-requests/second-per-thread limit) and
  `TEAMS_MAX_INLINE_TOTAL_BYTES` become one `ExtractLimits`, so an oversize image
  is refused *by the read* and keeps its markdown. Base64 inflation costs nothing
  here: Teams' ~100 KB activity-payload budget explicitly EXCLUDES a base64 image,
  so the picture limit is the only bound that binds.
- **One activity per image, sent after the text.** Teams SPLITS an activity
  carrying both text and an attachment and withholds its id, and its own guidance
  is to send separate activities rather than depend on that split.
- **The format allow-list is `{image/png, image/jpeg}`** — narrower than the
  neutral sniffer. Teams documents PNG/JPEG/GIF but states animated GIF does not
  render, and whether a GIF is animated is not decidable from leading bytes, so
  accepting the format would mean sometimes sending the one shape Teams refuses.
  WebP and BMP are not in its documented set. Pixel dimensions (Teams caps a
  picture at 1024×1024) are deliberately NOT pre-checked: it would mean decoding a
  header per format and would refuse an ordinary 1200-pixel-wide chart.
- **Every refusal is returned and audited, and no picture disappears in silence.**
  A neutral-module refusal (sensitive path, symlink, not-a-raster, over the per-file
  ceiling) keeps its original markdown, so the path stays visible. Two Teams-owned
  refusals cannot: `teams_inline_unsupported` (a real raster Teams will not inline)
  and `teams_inline_undelivered` (an activity that failed) are only knowable AFTER
  the read, by which point extraction has cut the markup — so those name the
  **resolved path** in their refusal line instead, keeping the same property by a
  different route. This is the one documented deviation from the neutral contract's
  "keeps its original markup". That line is therefore the ONLY surviving trace of the
  picture, so it is never budget-dropped: `_append_rejections` does not check
  `max_message_chars`, because every caller chunks and an over-cap body costs one
  extra message rather than a lost line. The undelivered-image follow-up is chunked
  for the same reason — a refusal quotes an LLM-authored path, so it has no bound of
  its own. Refusal lines are appended BEFORE display redaction, so a destination
  quoted in one is scanned like the rest of the answer.
- **The attachment NAME is a display sink too, and both its sources are untrusted.**
  `inline_image_name` prefers the (already redacted) alt text and falls back to the
  path's basename — which the model also wrote. `_SAFE_NAME_RE` preserves
  `[A-Za-z0-9._-]`, every character an `AKIA…` key id or a `ghp_…` token needs, and
  extraction has already cut the path out of the body, so for an empty caption this
  name is the only remaining sink. Both the SOURCE and the finished name are scanned
  and any hit collapses the whole name to `image.<ext>`; scanning the source too is
  not belt-and-braces, because the 64-char cut can slice a token down to a prefix the
  scanner no longer matches.
- **The approved root, and the one gate.** Extraction needs the provider's real
  `cwd`. `authorize_upload_root(root)` is the public setter a caller holding the
  live provider uses (same contract as Discord's renderer); absent that, the
  renderer resolves the SAME value lazily and off-loop from the session map's
  persisted per-session `cwd`, which is recorded from `provider.cwd` at session
  creation. It fails closed on every uncertain case — no row, a relative path, an
  unreadable map — because an unknown root means there is no boundary to check a
  reference against. Teams needs no analogue of Discord's restricted-session
  ceiling (a personal chat is already a 1:1 boundary gated by `allowed_emails`),
  but a `dashboard:`-namespaced key is refused anyway: a dashboard slot can be
  incognito and the renderer cannot resolve that signal. Audits carry counts and
  closed reason codes only, never the destination or a file name.

**Inbound needs `supportsFiles: true` in the MANIFEST.** Microsoft states plainly
that without it the file features do not work, and "receive files in personal chat"
is one of them — so an operator who skips it gets a bot that never sees a PDF, Word
document or text file, with no error and no refusal line, while pasted inline images
keep working (they arrive as an image `contentUrl`, not a
`file.download.info` attachment). The shipped guide lists it as a REQUIRED step for
that reason. Microsoft also does not support Teams file send/receive in GCC High,
DoD or 21Vianet.

**Ingest runs in the DISPATCHER, not the transport**, and the placement is
load-bearing rather than tidiness. It sits after the governance gate, after the
command intercept and after the busy check, so: nothing is fetched for a message
that ends up QUEUED (a mid-turn arrival may wait minutes), and the temp files are
unlinked by the same frame that awaits the turn reading them. Downloading at arrival
and unlinking in that frame left the drained turn a prompt naming a file that no
longer existed — `acp/prompt_blocks.py` skips a path that is not a file, so the model
received a bare `/tmp` path and answered about nothing, silently. Two rules follow
from the same place: an attachment-bearing message is never STEERED (a steer carries
text only, so the files would be dropped while the user is told they were folded in)
and never read as a COMMAND (Teams puts the caption in `text`, so "/stop here is the
log" would cancel the turn AND discard the file). The queue entry carries the RAW
descriptors and the drained turn re-ingests them, bounded by
`IngestLimits().max_attachments` so a burst is answered across turns instead of
having its surplus refused. Discord and Telegram draw every one of these lines in
the same place.

**Inbound: two kinds with OPPOSITE auth.** `TeamsInbound.attachments` carries
`activity.attachments` raw, and nothing is fetched until the personal-scope gate,
the identity resolution and the allow-list check have all passed in
`transport.receive` — the dispatcher, one layer in, is only reached after them, so an
unauthorized sender or a group conversation can never make the gateway fetch
anything. A file-only activity (attachments, no text) survives the empty-activity
guard, or the whole message would be discarded. Temp paths are owned by the frame
that awaits the turn and unlinked in a worker once it returns.

- **A personal-chat upload** arrives as
  `application/vnd.microsoft.teams.file.download.info`, whose `content.downloadUrl`
  Microsoft documents as a URL the reader "can issue an `HTTP GET` directly from"
  (the underlying Graph `@microsoft.graph.downloadUrl` says "Authentication isn't
  required with this URL" and is short-lived). It is fetched with **no credential,
  ever** — the Connector token is credential-equivalent and that host is not
  guaranteed to be one Microsoft operates.
- **An inline image** arrives with a `contentUrl`, and here Microsoft contradicts
  itself: the current page says the SDK handles authentication and its samples send
  no header, while the previous revision of the same page and the shipped sample
  attach the bot's Connector token. The host is not documented at all. So the
  decision fails closed on the host: the token is offered only to a recognized Bot
  Framework host (`_TOKEN_HOSTS_EXACT` / `_TOKEN_HOST_SUFFIXES`, dot-anchored so a
  lookalike cannot satisfy the match), and the anonymous fetch is tried as well, in
  that order. Both orders are documented as correct somewhere; trying both costs
  one extra request on a 401.
- **Bounds, in two halves.** `_vet_download_url` is the NAME check: https, no
  non-443 port, no IP literal, and not the loopback/link-local name space — with the
  FQDN root dot stripped first, because `localhost.` is the same host to every
  resolver and a blocklist comparing the raw name refuses one spelling while admitting
  the other. `_vet_resolved_address` is the second half, and it is the one a name
  blocklist cannot do: any public name an attacker controls can point at `127.0.0.1`
  or `169.254.169.254`, and a nip.io-style wildcard needs no control of a zone at all.
  It resolves through the single `resolve_addresses` seam (off-loop; also the one
  place a test can supply a record) and refuses if ANY answer is private, loopback,
  link-local, reserved, multicast or unspecified — stricter than "the one we would
  connect to", because the ordering aiohttp picks is not ours to predict. Resolution
  failure refuses too. It matters that this is a READ primitive, not a blind one: the
  body is written to a temp file and a text/document body is injected into the prompt,
  so the model would summarize an internal endpoint back into the chat.

  **The socket dials the address that was vetted.** Checking a NAME cannot close DNS
  rebinding: aiohttp resolves the URL host itself, so a pre-fetch vet and the connect are
  two lookups, and the second — microseconds before the socket opens — can answer
  `169.254.169.254`. So the vet RETURNS what it approved and the fetch pins it, and
  attachment fetches use their own `ClientSession` whose `TCPConnector` resolves through
  `_VettedResolver`: it serves pinned answers and refuses to resolve anything else, so
  there is no second lookup to poison and a code path reaching that session without
  vetting cannot fetch at all. aiohttp still sees the original URL, so TLS SNI and the
  `Host` header carry the real hostname — connecting by IP instead would break
  certificate validation. The pin map is bounded (`_PINNED_HOSTS_MAX`); an evicted entry
  costs a fresh lookup, never a weaker check, because the next fetch vets before it pins.
  The Connector session deliberately does NOT share this resolver: its hosts are gated by
  `connector_host_allowed`, a different and stricter rule, and routing them through a pin
  map would make an outbound activity depend on a download's state.

  Both halves run on EVERY redirect hop — at most three, followed MANUALLY so the
  credential decision is retaken for the host that actually serves the bytes — and
  `TEAMS_MAX_DOWNLOAD_BYTES` is enforced on bytes actually READ so a lying
  `Content-Length` cannot smuggle an unbounded body. Every write goes through a
  worker thread.
- **What is not a file.** Teams echoes rich text as a `text/html` attachment on
  ordinary messages, and a card can ride an activity; both are skipped without a
  note, because a per-message line would be pure noise. Any other unrecognized
  content type is reported by TYPE — not by file name — and never fetched.

`test/test_teams_attachments.py` pins the policy half (envelope mapping, the auth
flag per kind, the budgets, name sanitization, the refusal codes);
`test/test_teams_files.py` pins the wire half (the credential decision, URL
vetting, the read ceiling, the seal, and the inbound gate ordering).

## iMessage channel

**Transport (`kiro_crew/imessage/`).** A concrete `MessagingTransport` over the
external `imsg` CLI (MIT, macOS 14+) in its long-lived `rpc` mode: the gateway
spawns it as a child and speaks newline-framed JSON-RPC 2.0 over the child's
stdin/stdout, the same shape as a language server. No daemon, no port, no
webhook, and therefore **no new inbound network surface**; the child exits
cleanly when stdin closes, so the existing subprocess lifecycle applies
unchanged. `rpc.py` owns only the framing (request correlation, notification
routing, oversized/unparseable-line tolerance, a stdout limit far above
asyncio's 64 KiB default so one large line cannot kill the reader);
`client.py` owns iMessage semantics.

**Why an external bridge rather than Python.** iMessage has no server-side API,
so both halves a channel needs are macOS-native problems: following the Messages
SQLite database and its WAL through filesystem events (with a poll backstop,
because macOS drops events and rotates sidecar files) and dispatching a send
through Messages.app. A reimplementation would be a second, worse copy of a
moving target, and would put database-corruption and TCC-permission handling
inside the gateway process. The dependency is one binary the operator installs
with a package manager, and its absence is detectable and reportable at startup.

**Local-only is the design constraint, not a preference.** Hosted relays exist
that will hand you an iMessage-capable number and let any Linux host drive it
over an API. That is explicitly rejected: it puts a third party in the message
path of the one channel whose entire value is that the transport is the user's
own device and their own account.

**Inbound.** `watch.subscribe` on the all-chat stream with a `since_rowid`
cursor persisted to `$KIROCREW_HOME/imessage_cursor.json`, so a gateway restart
replays what it missed instead of losing it. The cursor advances on every
observed row, including ones the channel drops — a cursor that tracked only
delivered messages would replay every skipped row on the next start. Two
behaviours of the subscription are handled explicitly rather than discovered in
production:

- The subscription is **bounded** (`buffer_limit`, default 256). When it fills
  it ENDS, with one terminal `watch.overflow` notification carrying
  `resume_after_rowid`; the client resubscribes at that cursor with capped
  exponential backoff. Ignoring this makes the channel go permanently silent
  under a burst rather than lose one message.
- That cursor is at or before the first dropped message, so **duplicate replay
  is possible by design**. A bounded dedupe window keyed on message GUID
  (`DEDUPE_WINDOW = 1024`, deliberately larger than the buffer) is therefore
  required, not optional.

**Outbound.** `send` with a `to` handle. The result's `id`/`guid` are
best-effort in the bridge's own contract, so their absence is treated as success
with no id, never as failure.

**Typing and read receipts.** `typing` and `read` are documented exceptions to
the bridge's injected-helper requirement (typing keeps a direct-IMCore fallback,
read keeps bridge activation), so they work on a default install with
`bridge.ready = false`. Availability is probed from the `initialize`/`status`
readiness snapshot's `methods` field — the structurally usable surface at that
instant — and each degrades silently and permanently on first rejection, because
their parameter lists are not part of the bridge's documented surface. This
matters because iMessage cannot edit a sent message, so the typing indicator is
the only progress signal the channel has.

**Capabilities.** `streaming=False` and `edit=False` (no message mutation
exists), `reactions=False`, `files_inbound=False`, `files_outbound=False`,
`threads=False`, `max_buttons=0` (no tappable choices — a trailing `[OPTIONS:]`
trailer is stripped like on the other button-less channels),
`supports_proactive_send=True` (a Mac may message a handle at any time; there is
no 24-hour window), `supports_session_resume=False` (inbound routes off the
handle, not a mirrored session binding). `max_message_chars=4000` is declared
conservatively rather than measured: iMessage publishes no maximum, and this
field is a claim other code trusts, so under-declaring costs an extra message
while over-declaring risks a send the platform silently refuses.

**Access control.** Handle allowlist, deny-by-default — an empty allowlist
authorizes nobody, which is the correct posture for a channel with no org
boundary in front of it. Handles are normalized before comparison (email folds
to lowercase, phone loses formatting) so `+61 400 000 000` and `+61400000000`
are one handle. **Group chats fail closed** with a
`denied_group_chat` audit: a reply there would deliver tool output to members who
are not on the allowlist, the same reasoning that makes Telegram and Webex
direct-only. Unauthorized inbound is dropped with no reply, so an unknown sender
learns nothing about what they reached.

**Own-message suppression is TWO signals, and the gate order is load-bearing.**
Neither part is a defensive extra: a self-chat loop that answered its own replies
without bound shipped once (issue #5246), and each rule below is what closes it.

1. `is_from_me` drops the rows the bridge already attributes to the agent, without
   an audit event — the all-chat watch sees its own replies, and auditing them
   would log one entry per outbound message.
2. That flag is not sufficient on its own. In a **self-chat** the allow-listed
   handle IS the identity the agent sends as, and the bridge writes the
   attribution asynchronously (`watch.subscribe` defaults to a 500ms debounce
   expressly so an `is_from_me` correction can land), so an echo can arrive
   looking exactly like user input. The client therefore keeps a short-lived
   **ledger of what it sent** — one record per sent message, holding both the
   body it went out with and the guid the bridge reported, consumed whole on a
   match — and the transport consults it as the LAST gate before dispatch.

The ordering constraints are the part a refactor must preserve:

* The ledger check runs **after** the `is_from_me` drop, so a copy the platform
  already attributes to the agent cannot consume the record that the
  *unattributed* echo needs.
* It runs **after** the group and allowlist gates, because consuming is a side
  effect: a row that will be dropped anyway must not spend the record on its way
  out.
* A record's TTL starts when the send **resolves**, not when it is issued, and an
  unresolved record is never pruned or evicted — the echo of a slow send arrives
  before its result does.

Reordering any of those reintroduces the loop. The accepted cost is a bounded
one: an allow-listed sender who repeats the agent's exact text within the TTL has
that message suppressed once, in any chat rather than only a self-chat.

**Rendering.** Only the final answer is delivered; reasoning and tool activity
stay in the gateway. There is no placeholder message, because there is no edit
to rewrite it with — every other channel's "🤔 Thinking…" would be stranded
above the reply permanently. Markdown is flattened (`plaintext.py`) before
sending, with **fenced code-block contents passed through verbatim**: code is
what a user copies out of a message, and unwrapping or re-indenting it corrupts
it silently. Splitting runs last, on already-flat text, preferring a paragraph
break, then a line break, then a space, then a hard cut that still respects
grapheme clusters (a cut inside a flag, a skin-toned emoji, or a combining
accent renders as mojibake on both sides). CJK text, having no spaces, always
reaches the hard-cut path.

**Topology: v1 requires the gateway to run ON the Messages host,** and refuses
to start elsewhere. A gateway running remotely could point `cli_path` at a
transparent stdio wrapper and would appear to work — it can read chats and
process inbound — while outbound sends fail with an AppleEvents authorization
error (`-1743`), because the Automation grant is recorded against the
remote-shell server process, which macOS exposes no grantable toggle for.
Shipping that topology would mean shipping a send path that cannot be made to
work, so the channel refuses and reports why.

**Host requirements.** macOS 14+ with Messages signed in; Full Disk Access for
the process context that reads the Messages database; Automation permission for
Messages.app for sends. Both grants are per process context, so a headless
launch-agent gateway needs its own one-time interactive grant.

**Deliberately out of scope for v1.** Group chats, attachments in either
direction, SMS-only operation, and every message mutation (tapbacks, edit,
unsend, effects, polls, group management). Those last ones require injecting a
helper into Messages.app, which requires System Integrity Protection to be
disabled system-wide. **v1 must not require SIP changes** — asking a user to
disable SIP to talk to their own agent is not an acceptable default.

**Pod isolation.** `pod/runtime.py` forces `imessage.enabled = false` in a
sanitized seed. iMessage is the one channel with no credential to scrub, so a
pod that inherited `enabled: true` from a real config would drive the operator's
actual Messages.app and reply to real people.

## iMessage settings API

- `GET /api/imessage/config` — `connected` (true only while the bridge's watch
  is live this session, kept truthful by `IMessageClient.on_state_change`),
  `connect_error`, `configured` (enabled AND a non-empty allowlist — the
  transport fails closed on an empty list), `supported` (false off macOS, so the
  UI can explain the requirement instead of leaving the operator to infer it
  from a channel that never connects), `read_only` (true unless the request is
  direct-local), plus `enabled`, `cli_path`, `db_path`, `allowed_handles`,
  `service` and `session_folder`. **There is no credential in this payload** —
  no mask, no presence boolean, nothing to rotate.
- `PUT /api/imessage/config` — requires a direct-local request (loopback peer
  AND no forwarding headers); remote gets 403. Validate-first/commit-last.
  `allowed_handles` accepts an Apple Account email or a phone-shaped handle
  (linear string checks, no regex, so an operator-supplied list cannot trigger
  polynomial backtracking). `service` must be one of `imessage` / `sms` /
  `auto`, sharing one `IMESSAGE_SERVICES` constant with the loader's clamp so
  the form's choices and the config normalization cannot drift. `cli_path` and
  `db_path` reject line breaks and NULs: they become `argv` of a spawned child
  (via `create_subprocess_exec`, never a shell), where a newline would corrupt
  the argument rather than be quoted. Writes go to `config.json` under
  `imessage`, serialized under the repo-wide config lock. Every field except
  `session_folder` is boot-read, so `restart_required` is true on any other
  change.

## WhatsApp channel

A QR-linked **personal** account, paired as a linked device over the WhatsApp Web
protocol (`neonize`, an optional extra installed with `kirocrew[whatsapp]`). There
is no bot identity and no Business account, so the agent sends **as the
operator**, which is what makes the two invariants below load-bearing rather
than tidy.

Because it is the personal Web protocol and not the Business Cloud API, figures
quoted for the Cloud API do not transfer in either direction: there are no
interactive reply buttons at all, and there is no 24-hour customer-service
window.

**Echo discipline** (`whatsapp/echo.py`). Every message the account sends comes
back on the event stream with `from_me=True`, byte-identical in shape whether the
operator typed it or this channel sent it. Content matching cannot separate them
(the operator may quote the agent; a prefix marker leaks into the conversation),
so the channel tracks the **message ID of every send** and drops an inbound
`from_me` event whose ID it remembers. A `from_me` message it does NOT remember is
the operator typing, which is the self-chat command surface. Reads are
non-consuming because WhatsApp redelivers after a reconnect, and a one-shot pop
would let the redelivery through as a phantom operator command.

**The session store is on the sensitive keystone.** `<data home>/whatsapp/` holds
whatsmeow's device keys, which are the entire credential: anything that reads them
can act as the operator on WhatsApp with no second factor. It is a
`_CREW_SECRET_LEAVES` entry, classified as the DIRECTORY so the SQLite WAL and SHM
sidecars are covered too, and the path is pinned to the default: `whatsapp.db_path`
is inert, because the protection is a path match and an operator-supplied location
would carry the credential out from behind it.

**Identity is folded at the edge** (`whatsapp/transport.py::_canonical_sender`).
Multi-device addresses a sender either by their phone number or by a Linked
Identity (`<id>@lid`), and the two user parts are UNRELATED strings. Operator
config is written in phone numbers, so an `@lid` sender is resolved to its phone
JID once per sender (`client.phone_for_lid`, cached) before anything compares it
to the allowlist. Without that fold, `dm_policy="allowlist"` silently ignores a
person the operator explicitly allowed: it fails closed, which is the safe
direction, but presents as the channel being broken. An unresolvable alias keeps
the `@lid` form and therefore stays denied.

**Capabilities.** `streaming=True`, `edit=True`, `reactions=True`,
`files_inbound=True`, `files_outbound=True`, `rich_blocks=False`,
`threads=False`, `max_message_chars` reads `renderer.WHATSAPP_CHUNK_LIMIT`,
`max_buttons=0`, `supports_proactive_send=True` (no 24-hour window, so reminders
and cron results deliver at any time), `supports_session_resume=False` (inbound
derives its key from the chat JID and never resolves a dashboard mirror binding,
so a dashboard connect is outbound-only).

`max_buttons=0` is a conservative CHOICE, recorded as unverified rather than as a
platform ceiling. The pinned wheel ships a complete interactive-message builder
(`neonize/ext/interactive_message/`, `send_interactive_message`) and a poll builder
(`build_poll_vote_creation` / `decrypt_poll_vote`); what nothing in this repo could
establish is whether a recipient's client RENDERS a native-flow message sent from a
personal linked device rather than a Business account. Writing it down as
impossible would close the door on every future picker on this channel.

**A group member is admitted to the conversation, not to the machine.** Step 5 of
the gauntlet authorizes the group SURFACE, so a configured group never reaches
`authorize`, and membership alone would let any member trigger an authenticated
whole-blob download into the gateway's heap at will: in `rules` mode an unaddressed
message already answers `respond=True`, and the per-group cooldown does not bound
the fetch because it only starts once a reply actually delivered, which a
sentinel-silenced turn never does. `_may_fetch_media` therefore requires
INDIVIDUAL admission for group media (the linked account, or a number the operator
listed) and deliberately does not consult `dm_policy`, because `open` resolves to
"anyone with a user id" and would hand the capability straight back. A refusal is
spoken through the same note path an unsupported type uses, since silence reads as
the agent ignoring a photo the sender believes it received.

**Streaming is by edit, throttled.** The Web protocol exposes an edit where the
Business Cloud API does not, so the renderer sends the first bubble once there is
something worth reading and then edits it, opening a new one when the text
outgrows a message or the 20-minute edit window closes. Two bounds exist for the
operator's phone number rather than for looks: edits are coalesced to at most one
per `_EDIT_INTERVAL_S`, single-flight, keeping only the newest pending text (each
edit is a full end-to-end encrypted send to every device of every participant, and
neonize's own example edits once per character); and past the window the server
refuses, so the bubble seals instead of silently dropping progress. An unprompted
group turn never streams, because it may still choose silence and a streamed
prefix cannot be unsent.

**Media.** Inbound images, stickers, voice notes, audio and documents go through
`whatsapp/media.py` (which decides what arrived, from the protobuf alone) and
`whatsapp/attachments.py` (which fetches the bytes into the shared ingest path).
Presence is probed with `HasField`, never truthiness: a protobuf's singular
submessage is never `None`, so reading an absent one returns a default instance
and truthiness reports every field present. Content that cannot be ingested (a
location, a poll, a contact card) produces a visible note rather than silence.
Outbound, `whatsapp/files.py` runs the shared extractor at the seal and uploads
each raster from BYTES, never by re-opening the path, because every security gate
was applied to one inode. Its ceilings are this repo's policy, not sourced
platform figures.

**An inbound name is the sender's CLAIM, and the ingest layer reads it.** The
shared layer picks a document parser and a transcription decoder from the
filename's extension, so `whatsapp/attachments.py` appends a mimetype-derived
suffix only when the name carries none. A document is the one kind that arrives
with a filename at all, and a declared type is only a claim: a PDF sent as
`application/octet-stream` derives `.bin`, and appending that turns `report.pdf`
into `report.pdf.bin`, which matches no parser, so the file is refused before it
is ever downloaded. The pinned `_SUFFIX_OVERRIDES` entries outrank the name as
well, for the reason they exist: an `audio/ogg` attachment the sender named
`note.oga` reaches the same suffix the transcription backend does not recognise.
`MAX_MEDIA_BYTES` is enforced twice, against two different values, because
neither alone is enough. The pinned binding returns the whole decrypted object in
one value (it exposes no streaming and no size-limited download), so the ONLY
bound that can act before the allocation is the sender's DECLARED `fileLength`,
and the only bound that can be trusted is the length of what actually arrived.
The pre-fetch check therefore refuses an oversized declaration, refuses one that
would not fit in `host_available_mib()` with headroom (a 0 reading means "could
not determine" and allows the fetch, never "no memory"), and refuses a media
message that declares NO length at all: every media protobuf carries the field
and `describe` reads it for every kind, so an absent one is not a legitimate
shape, and it is precisely the input that makes the shared layer's
`att.size and att.size > cap` pre-check skip itself. The post-fetch check is what
keeps an UNDERSTATED object off the disk and out of the prompt. What remains, and
is a binding limitation rather than a policy choice, is that an admitted sender
who understates the length still causes one transient allocation bounded by
WhatsApp's own server-side media cap. The write to the shared temp path is
offloaded because `TMPDIR` is not guaranteed to be local disk.

**Outbound media protobufs are assembled by the channel, not by neonize's `send_*`
helpers.** `send_image_bytes`, `send_voice_bytes` and `send_document_bytes` each
build their own `Message` and go out through one `send_message`, for the same
reason `send_text` passes an explicit `Message(conversation=...)`:
`build_image_message` and `build_document_message` run neonize's mention parser
over the CAPTION and put the result in `contextInfo.mentionedJID`, so an
`@<digits>` run in agent-authored alt text would be delivered as a real mention of
that number and would notify its owner in a group. The captions carry an empty
`ContextInfo` instead. The image build also decoded and rescaled the whole raster
inline to make the inline preview; that decode runs through `asyncio.to_thread`,
and the media type is declared per upload so neonize never probes the bytes with
libmagic on the loop. `build_audio_message` is avoided for a different reason: it
shells out to `ffprobe` for the duration, which would make an FFmpeg install a hard
requirement for sending a voice note, so `AudioMessage.seconds` comes from the
caller or from a stdlib WAV header read and degrades to an unlabelled duration. A
voice note is `PTT=True`, which is what makes the recipient's client draw a player
rather than a file attachment; the container is the caller's to choose, and
WhatsApp's own voice notes are `audio/ogg; codecs=opus`, so a client may decline to
play push-to-talk audio in another format. A document travels under the BASENAME of
its path only, because WhatsApp shows the recipient whatever `fileName` carries.

**Access control.** `dm_policy` is deny-by-default with four values: `self`
(the default; only the linked account's own messages), `allowlist`, `open`, and
`disabled`. An unrecognized value denies everyone. Groups are invisible
unless configured per group, then gated by mode, mention and cooldown
(`whatsapp/group_gate.py`).

**A stale group entry is reported once, at the first connect.** Groups are opt-in
and matched by exact JID, so an entry the account cannot resolve (a hand-typed
JID, or a group the account has since left) is INVISIBLE rather than broken: the
gate drops every message from a JID it does not hold, and silence is
indistinguishable from nobody writing. On the first transition to `connected` the
channel diffs the configured JIDs against `client.list_groups()` and logs ONE
aggregated warning naming the unmatched ones. Three properties are load-bearing.
It hangs off the CONNECTED state callback rather than off `connect()`, because
`connect()` returns once the attempt is merely underway, so a diff taken there
sees no joined groups and would name every configured group on every boot. It is
scheduled as a task rather than awaited, so the round trip never lands inside the
sequence that starts the remaining channels. And it compares exact strings, the
same key `GroupGate` indexes its entries by, because a normalizing compare would
call a case or `:device` typo fine and leave the group mute. An empty
`list_groups()` answer produces no warning at all: that value means both "the
account is in no groups" and "the `get_joined_groups` call failed", so it cannot
tell a stale JID from a probe that never ran, and naming every configured group on
a transient API failure is what teaches an operator to ignore the line.

Proactive targets are gated by MEMBERSHIP, not just by shape.
`resolve_configured_target` reads `_proactive_targets()`, the same linked-account
plus DM-allowlist pair `configured_targets` lists from, so what the dashboard
offers and what it accepts cannot drift. That resolver is the only allowlist check
on the mirror-link path, which round-trips a chosen id back through it, and
`dm_policy` never sees that path: accepting any well-formed `user:<number>` would
let a proactive send open a conversation with an arbitrary phone number.

**Acting as the agent is narrower than talking to it.** `is_operator` admits only
the linked account,
independent of `dm_policy`: `open` admits a stranger to CHAT and a configured
group admits its members, but neither may authorize a command on the operator's
machine. It reads the operator verdict `receive` already reached from the full
multi-device picture rather than re-deriving it from the bare user part left on
`InboundMessage`. With `max_buttons=0` the prompt is the numbered text form from
`messaging/approval.py`, answered by typing `1`, `2` or `3`; silence denies. A
reply is consumed only while a request is actually open for that session, so
`no` is an ordinary message at every other moment, and only a reply that is
ENTIRELY a verdict counts, so `no, use the other file` reaches the model.

**A `from_me` message is not automatically a command.** It means the ACCOUNT sent
it, which includes the operator texting an ordinary contact from their phone, so
operator authority in a DIRECT chat requires the SELF-chat: that is the command
surface. Answering an outgoing message to a contact would put the agent into the
operator's private conversation and reply in that contact's chat. A configured
group is exempt, because that is where the operator does address the agent, gated
by mention or rules.

**A non-operator never shares the operator's unified session.**
`messaging.dm_scope="unified"` collapses every direct DM into one
`unified:{agent}` bucket, which is right for the operator (their WhatsApp and
dashboard conversations are the same conversation) and wrong for anyone else,
because that bucket carries the operator's history: an admitted peer could ask
what was discussed earlier and be told. A non-operator always gets a per-peer
bucket whatever the global setting says.

A non-operator's turn additionally carries `ChannelTurn.deny_all_tools`, because
setting the approval mode is not enough: the PreToolUse hook can answer
`auto_approve` and a session carrying Trust short-circuits, both ahead of the
interactive ladder. They may talk to the agent; they cannot make it act. Steering
is gated the same way, since it injects text into a turn already running, which
under a unified DM scope is the operator's.

**Private context is withheld per SESSION, not per sender.** `minimal_context` is
`group or not is_operator`, and the `group` half is the one that is easy to get
wrong: a group's session key IS the group, so one session serves every member.
Keying the decision on who typed the current message leaks one turn later, because
the operator addressing the agent in that group injects their memory, lessons and
skills into the shared session and ACP replays native history, so a member's own
minimal-context turn can still be answered out of it. A group turn is therefore
minimal for EVERYONE, the operator included. The cost is deliberate: the self-chat
and DMs are where context-rich work belongs, and anything the agent says in a group
is visible to the group regardless.

It also carries `ChannelTurn.minimal_context`, and that is a SEPARATE control
rather than a duplicate of the two above. Both of those govern what the turn may
DO; this one governs what the turn is TOLD. The context builder assembles memory,
lessons, skills and history into the prompt before the model runs and before any
tool is requested, so a peer admitted only to chat would otherwise be answered
out of the operator's private material, with no tool call and no approval prompt
anywhere on the path to notice.

**The generation counter is seeded from disk** (`ConversationState(seed_fn=…)` →
`link.seed_generation`), like every sibling channel's. It is in-memory, so an
unseeded counter restarts at 0 after a gateway restart and the next `/new`
advances 0 → 1 straight back onto the `:gen1` still persisted, resuming the
conversation the operator explicitly discarded. The seed must address the same
bucket `_session_key` builds, so the chat type is re-derived from the JID (a group
keeps its full forum bucket whatever `dm_scope` says). It uses the OPERATOR's
`dm_scope` because `seed_fn` receives a scope with no sender attached, and the two
readings fail in opposite directions: reading the operator's bucket for a peer's
scope over-seeds, which merely skips generations and still yields a fresh session,
while reading a per-peer bucket for the operator under `dm_scope="unified"` reads
a bucket their conversation does not live in, answers 0, and puts the
resurrection straight back.

The same predicate gates COMMANDS, which is not interchangeable with the group
steer verdict: a DM carries no verdict, so the steer gate alone answers yes for
any admitted sender, and with `messaging.dm_scope = "unified"` every direct DM
shares one `unified:{agent}` bucket. Under `dm_policy` `allowlist` or `open` a
peer's `/new` would therefore bump the generation on the conversation the
operator is using.

**A group cooldown follows delivery, not the renderer's own bookkeeping.** A
muted conversation never runs this renderer: `drive_turn` substitutes
`SilentRenderer` into its LOCAL name, so `on_done` is called on that object and
the channel renderer's `suppressed` flag stays False. Recording the cooldown on
`not suppressed` therefore consumed it for a reply nobody received, silencing the
next unprompted turn that had something to say. The renderer sets `delivered`
only after a send returns, which also covers a send that raised.

**Phase reactions report the OUTCOME, and that is not the complement of
`delivered`.** With `max_buttons=0` a reaction on the operator's own inbound
message is the channel's only at-a-glance progress marker, so the dispatcher
draws the hourglass before the turn and the tick or the warning sign after it.
The success test is `delivered and not failed`, never `delivered` alone: a failed
turn still SENDS something, the apology notice, so `delivered` is True there too
and reading it by itself stamps a failure with the success tick. Only the renderer
sees the stop reason, so it records `failed` in `on_done`; the dispatcher cannot
re-derive it from what reached the chat. `_react` is the single chokepoint and
carries both suppressions: an unprompted group turn (a reaction is still a visible
mark in a conversation the agent was not addressed in) and a muted conversation
(`SilentRenderer` means these flags describe nothing that was sent, and a mute the
operator asked for must not answer back with a warning sign).

**Rendering.** `to_whatsapp_text` converts Markdown into WhatsApp's dialect
(`*bold*`, `_italic_`, `~strike~`, one code marker and no info strings) and
`render_chunks` splits with the shared `split_markdown_safe`. Fence grammar comes
from `messaging/split.py::iter_fence_lines` rather than a channel-local backtick
counter, which got three things wrong that reached the user: a four-backtick
block was not recognized as code so Markdown inside it was rewritten, a `~~~`
fence was not recognized at all, and hard-splitting a long block left chunks
carrying an odd number of delimiters, which WhatsApp renders as a monospace block
that never closes. Every rewrite runs BEFORE splitting so the splitter measures
what is delivered, which is what lets a step GROW the text: a flattened table row
carries its column labels, a diagram gains a heading, and a redacted credential
becomes a marker longer than the key. Splitting is awaited off the loop, as
Discord does. The full order is
`strip_ansi -> screen -> reduce -> convert -> screen -> split`.

**The reductions live in `messaging/markup.py`, and they emit Markdown.** This is
the most mobile surface in the product, so a construct that needs a monospace grid
or an image is unreadable here rather than merely plain, and the dialect drops
every info string. Three are reduced: a `<thinking>` block (no chat platform folds
one away, so the model's scratchpad is otherwise part of the answer), a pipe table
(flattened to one labelled bullet per row, the shape Slack adopted for mobile
readability), and a `mermaid` fence (flattened to arrows under a `*Diagram*`
heading, or kept as source under that same heading when the grammar is not one the
reduction reads). Each emits Markdown rather than dialect, so `_convert_line`
stays the single place that knows WhatsApp's spelling and the module stays usable
by the sibling channels that have the same gap. Two rules keep the table reduction
from losing an authored line, which the Slack copy it was modelled on does: a
separator row is REQUIRED before a run of pipe lines counts as a table, and a
table with no data rows is passed through verbatim. Tables are reduced per
non-fenced RUN, so a code sample full of pipes is never rewritten, and the mermaid
info string is read off the OPENER through the shared fence machine, so a mermaid
fence nested in a wider block stays content.

**The renderer is a registered `security_posture` redaction sink, and the screen
that carries the guarantee runs LAST.** This is the only markup-CONSUMING channel
whose own converter rewrites the delimiters: `TurnDriver` scans the provider stream
as literal bytes, so `AKIA**I**OSFODNN7EXAMPLE` matches no credential pattern,
`to_whatsapp_text` turns it into `AKIA*I*OSFODNN7EXAMPLE`, and the reader's client
strips the markers and shows an intact key. `to_whatsapp_text` therefore screens
through `display_safety.redact_for_display` AFTER every reduction, which is the
only form whose safety does not depend on which reductions ran: each of them
deletes a span, and deleting a span joins what sat on either side of it. A scan of
`AKIA<thinking>x</thinking>IOSFODNN7EXAMPLE` sees nothing at all, which is why
screening before the transform is the bypass rather than the fix. A second screen
runs before the conversion; it is a belt, and it also carries the ANSI
normalisation the conversion depends on, since every dialect rule is line-shaped
and an escape in front of a `#` hides the heading from it. `display_safe_text` is
the same screen without the conversion, for the sinks that do not pass through
`render_chunks` at all: the whole-reply fallback, a file-rejection note, an image
caption, and the approval prompt (whose tool title is model-authored and is
interpolated verbatim by `build_approval_prompt`). A screen on the chunk path alone
would leave all four as the bypass. The streaming and final paths are two more, and
both reach `render_chunks` through `_rendered_chunks`, which is why one screen
covers them.

**Inline code is byte-exact through the conversion**
(`renderer._sub_outside_code`). The dialect has no escape character, so a backtick
span is the only way to show text WhatsApp must not reformat, and `__` inside
`` `/tmp/__init__.py` `` is a filename rather than emphasis. The guard tests the
DELIMITER positions rather than overlap, because emphasis legitimately spans a code
span (` **a `b` c** `) and skipping the whole match there would leave visible `**`
litter. `files.py::rejection_note` is built on this: the renderer's send path
converts everything it puts on the wire (`transport.send_message` →
`client.send_text` → `render_chunks`) and there is no unconverted send to reach
for, so the note is written to survive that round trip byte-identically instead.

**`_show` is the only thing that may advance `_sealed_count`.** Its contract is
"this chunk is on screen, and only then does the count advance", and it exists
because three call sites independently reached the same wrong conclusion: a closed
edit window, a refused streaming edit, and a refused final edit each counted a
chunk that never arrived, and once the count passes a chunk no later flush and no
`on_done` pass looks at it again, so the middle of a reply disappears with nothing
raised or logged. There is exactly one branchy fact, an edit can be refused, and
a refusal closes the bubble, so it is answered once. A replacement bubble repeats
whatever prefix the sealed one still shows, which is the right trade: a duplicated
sentence is visible and recoverable, a silently missing one is neither. The tail is
the one placement that does NOT advance the count, because the splitter can still
revise it; `_seal_chunk` advances in a `finally`, so a send that raises is not
retried on every later flush for the rest of the turn.

**`_rendered_chunks` detaches the protocol suffix, and that is what keeps it
MONOTONIC.** `_strip_options` only removes a COMPLETE trailer, so a still-arriving
`[OPTIONS: yes | n` renders into the live bubble as litter, and the length goes
with it: 4,089 visible characters plus that fragment is two chunks, while the
completed ` [OPTIONS: yes | no]` strips back to one. A flush landing in that window
leaves `_sealed_count` permanently above `len(chunks)`, so every later flush
returns at `_render_live`'s guard and `on_done` computes an empty pending slice:
the fragment stays on screen and the rest of the reply is dropped. Discord and
Telegram detach the same suffix at the same point with `split_trailing_protocol_suffix`.

**The composing indicator is dropped while an approval is pending.** `_hold_typing`
refreshes every 8 seconds and is otherwise cancelled only by `on_done`/`close`,
while the approval window is 300 seconds, so the operator would watch "typing" for
five minutes while the agent is in fact waiting on THEM. `on_prompt_choice` stops
the refresh once the prompt lands, and `_resume_typing` re-arms it lazily from
`on_text_chunk` and `on_tool_call`, because nothing reports a decision back to a
renderer: the driver dispatches `PROMPT_CHOICE` and only then awaits the decider,
so the next output event is the first news the wait is over.

**A timed-out approval is spoken, in place.** `on_prompt_choice` keeps the prompt's
own message id and registers `approval.PendingApproval.on_timeout`; when the window
closes the renderer edits that bubble to `approval.TIMEOUT_NOTICE`, falling back to
a fresh message when the edit is refused or its own window has closed. Without it
deny-on-silence is invisible: the tool is refused, the turn moves on, and a
live-looking prompt sits in the chat that a later `1` can no longer answer (it
finds no open entry and is reported as expired). The hook is a callback rather than
a transport because `approval.py` never learns what a channel is, and the renderer
that posted the prompt is the only thing that knows which bubble to edit; it is
awaited from `wait()`, so it never raises, a notice that could not be posted must
not become an approval.

**Context accounting rides `ChannelTurn.notice`, which is the channel's ONLY
reach into it.** `_maybe_notice` calls `sessions.check_context_usage`, and that
is the sole caller of the session manager's compaction trigger, so a
shared-pipeline channel that omits `notice=` has no compaction of any kind:
neither the threshold nudge nor the backend autocompactor. Past
`whatsapp.hard_threshold_pct` the context is compacted in place immediately
(the backend threshold sits higher), and past `whatsapp.soft_threshold_pct` the
operator is nudged once toward `/compact` or `/new`. The reading and the
compaction are session hygiene and run for every turn, because an overflowed
window costs the operator the conversation; only the TEXT is conditional, on the
same two rules `_react` carries -- an unprompted group turn may still be choosing
silence, and a muted conversation is one the operator switched off. The
awaiting-compact flag records that the nudge WAS SENT, so it is set only when one
goes out: setting it while suppressed would spend the single nudge a conversation
gets on a message nobody read. `/compact` and a fresh generation both clear it.

**`/compact` compacts, and its receipt describes what already happened.** It runs
in place under atomic `try_acquire` (compacting while a turn is mutating the same
session interleaves JSON-RPC on one provider and races the transcript), and a
refused acquire is disambiguated with `has_session`: a turn holds the session
(ask again) or there is no session (nothing to compact). Telling the operator the
wrong one, or acknowledging with a promise to compact later, leaves them waiting
for a compaction that never happens -- and on this channel there is no context
indicator to check it against.

**Commands** come from the table in `whatsapp/commands.py`, which is also what
`/help` is derived from, so a command cannot ship undocumented: `/new`,
`/compact`, `/help`, `/status`, `/stop`. Matching is whole-message and exact,
which is load-bearing rather than fussy: `receive` drops a non-operator group
message when `parse_command` is truthy, so a prefix match would start swallowing
ordinary group messages that merely begin with a slash. The table decides
operator-only per command, which is what lets `/help` stay answerable while
everything that acts on the session does not.

**Still not at parity with Slack**, listed rather than implied: rich blocks and
modals (no such surface exists), threads (quoting is not a thread), an App Home
equivalent, and inbound reaction events as a control channel. Read receipts are
deliberately absent rather than pending: `client.mark_read` exists, but calling it
writes to the operator's own account and overrides whatever read-receipt privacy
setting their phone carries, so it is a product decision and not a parity gap.

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

**Redelivery suppression.** lark's WS may redeliver an event, so a bounded
message-id window lives in `FeishuTransport.receive` and is consulted as the LAST
gate — after the chat-type gate, the group allow-list and `authorize` — per the
cross-channel invariant above. Placed any earlier it is self-defeating: traffic
the bot can see but will never serve (an unauthorized sender, a group it merely
sits in, a sticker) would occupy the window, and crossing the cap trims 500 to
200 in one pass, so the id evicted is an authorized one and its redelivery
re-runs a turn's tool side effects. A frame with no `message_id` is dispatched
and never recorded — no id is no evidence of a duplicate, and inserting an empty
key would make every id-less frame a duplicate of the first. The window needs no
lock: `receive` runs on the event loop (the WS thread hands off via
`run_coroutine_threadsafe`), and the membership test and insert are not separated
by an `await`.

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
context-threshold notices as separate proactive messages). The soft notice is
latched per route and released on BOTH the hard-threshold compaction and a
manual `/compact`, so it stays a per-growth-cycle nudge instead of decaying
into a once-ever one. `messaging.idle_reset_minutes` / `daily_reset_hour` are
honoured through `ConversationState.maybe_rotate`, called AFTER the busy check
— rotating first would mint a new generation and miss an in-flight turn on the
current key — with the session key re-derived afterwards. The turn carries a
`build_directive_consumer` so the session-directive tools (`monitor_start`,
`autonudge_stop`, …) apply against the Feishu session rather than returning a
marker the driver leaves inert while still reporting success to the model;
dashboard-only directives stay refused for a channel turn (`slot=None`,
fail-closed). The dispatcher runs decider-less (no interactive buttons); an
`[OPTIONS:]` trailer degrades to a numbered text list through the shared
`render_options_as_text`. The renderer buffers the complete turn and sends it as
one reply on `on_done` — no streaming, no edit-in-place. Because nothing is shown
before `on_done`, an UNFINISHED `[OPTIONS` tail is kept rather than cut: there is
no partial frame for it to flash in, so it can only be the assistant's own prose.

**Session identity.** A p2p turn keys on `open_id` under the `DIRECT` chat
type; a group turn keys on the group's `chat_id` under the non-direct
(`FORUM`) chat type, so a group turn never resumes the sender's private DM
bucket — including under `dm_scope: unified`, where a direct key collapses to
the cross-channel bucket and a group key deliberately does not.

**A group turn is minimal-context for everyone.** The key above isolates the
turn HISTORY, which is only half the disclosure: memory, lessons and skills are
assembled by the context builder, not read from the session, so a group turn
runs with `minimal_context=True` regardless of who sent it. The property that
matters belongs to the CONVERSATION rather than to the sender — a group's key is
the group, so one session serves every member, and an allow-listed sender
addressing the agent in a group would otherwise inject their private context
into a reply the whole group reads. The p2p session is where context-rich work
belongs. WhatsApp's extra `not is_operator` clause does not transfer: its
transport is the operator's own account and can tell the operator from a peer,
whereas Feishu has a bot identity and authorises against `allowed_open_ids`,
where every admitted DM sender is an equally-trusted peer with their own
`open_id`-keyed bucket.
