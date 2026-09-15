# Side Conversation Module

## Overview

The side conversation module adds an ephemeral Q&A thread to a parent chat
slot. Users invoke it via the `/side` command or the "Side Chat" tab in the
Activity panel — the user-facing name is Side Chat, while `side` remains the
internal spelling for the tab id, state, routes and this module. The `/side`
command is intercepted client-side regardless of
the parent turn's state: while a turn is running, the composer's steer path
checks `isInterceptedSlashCommand` before steering, so the command opens the
side chat instead of being injected into the running turn as literal text.
Paste tokens are expanded before the command is delegated, and a rejected
command (side turn already in flight, question over the byte limit) is
merged back into the originating slot's composer or persisted draft so the
question is never silently lost.
The side runs against the same parent slot identity but
spawns its own isolated LLM session, reads parent context as a frozen
snapshot, and never persists messages to JSONL or memory stores.

Design strategy: Option C (sidecar storage on parent slot) with a native
implementation lifting wire format and system prompt strings from the
upstream OpenClaw `/btw` protocol.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (KiroCrewWebsite)                                     │
│  ┌────────────┐  ┌────────────────┐  ┌───────────────────────┐ │
│  │ SideChat   │→ │ chatSlice      │← │ useWebSocket          │ │
│  │ .tsx       │  │ slotSide state │  │ chat.side_result case │ │
│  └────────────┘  └────────────────┘  └───────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
         │ HTTP                              ▲ WS
         ▼                                  │
┌─────────────────────────────────────────────────────────────────┐
│  Backend (KiroCrew)                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │ handlers/    │→ │ side_state   │  │ ws.py                 │ │
│  │ side.py      │  │ .py          │  │ broadcast_side_result │ │
│  ├──────────────┤  ├──────────────┤  └───────────────────────┘ │
│  │ side_context │  │ side_prompts │                             │
│  │ .py          │  │ .py          │                             │
│  └──────────────┘  └──────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

### Key Invariants

1. **No new slot identity** — side lives as `slot._side: SideState | None`.
2. **Main path byte-frozen** — `context.py.build_message()` and
   `dashboard/chat.py` main-thread paths are never modified.
3. **Birth-only memory mode** — side never calls memory/learn/save; the
   sidecar buffer is discarded on close with no persistence.
4. **Isolated LLM session** — keyed on `f"side:{slot.key}"`, separate from
   the parent's session, so turns don't pollute parent context.
5. **Non-blocking** — `api_side_turn` returns immediately; streaming runs
   as a background task.
6. **A submit is never dropped** — while a turn is in flight the message is
   steered into it or queued behind it; there is no rejection path.

## Busy-send: steer and queue

A side turn is a real LLM turn, so a second question can arrive while one is
streaming. The sidecar handles it with the same two-mode contract as the main
composer, and the frontend reuses the main chat's split send button
(`components/BusySendButton.tsx`) and queue cards (`components/QueueStack.tsx`)
so the two surfaces cannot drift.

| Mode | Effect |
|------|--------|
| `steer` (default) | `POST .../side/turn` with `{"steer": true}` injects the text into the RUNNING turn via the isolated session's `steer()` RPC. |
| `queue` | The text is held on `SideState.queue` and dispatched as the next turn when the current one ends. |

Which mode Enter takes is a PER-SLOT preference (`mc-busy-send-mode:<slot>` in
localStorage), shared live between one session's main composer and its side
panel; other sessions keep their own mode. A slot that has never chosen a mode
inherits the legacy unscoped `mc-busy-send-mode` value.

Fall-through, not rejection: a steer is attempted only when the isolated session
exists, reports `supports_steer`, and `has_active_turn()` is true. Any of those
failing — or the RPC returning False — falls through to the queue. kiro-cli
silently swallows a steer aimed at a prompt that already ended, so without the
liveness probe the text would vanish with no turn and no queue entry.

The commit is bound to the sidecar **object** and the `run_id` captured BEFORE the
RPC suspends. Re-reading them afterwards would misattribute the steer: a
close+reopen swaps in a fresh `SideState` that is also `open`, and a finished
turn's drain can already have started the next run. So after the await —

- sidecar replaced (or closed) → 409; the replacement never asked for the text.
- same sidecar, run advanced or completed → the turn's `finally` owns the entry;
  the response reports `queued` + `demoted` rather than adding a second copy.
- same sidecar, same run, still incomplete → committed as a steer.

**A steer is never proven delivered by the RPC.** `steer()` returning True only
proves the bytes left the process; the backend's `steering_consumed` echo is the
authoritative signal, surfaced through `stream_and_collect`'s
`on_steer_consumed` hook.

Delivery state lives in an explicit **ledger**, `SideState.steers`, holding
`{id, text, state}` with state `pending` | `consumed` | `requeued`. The state is
explicit rather than encoded as presence-in-a-list because a submitter has to tell
three outcomes apart — *delivered and answered*, *never injected*, and *already
turned into a queue card* — and an absence cannot distinguish the first from the
third.

- registered BEFORE the RPC suspends, so a turn that ends during it already sees
  the entry;
- the echo **marks** `consumed` (never erases — an erased entry cannot tell a
  waiting submitter that its steer landed);
- the turn's `finally` marks whatever is still `pending` as `requeued` and puts it
  at the HEAD of the queue as an ordinary, cancellable card;
- the submitter reads back **its own id**, and `consumed` **outranks turn
  completion**: a question the backend injected is committed to the transcript even
  if the turn has since ended, because reporting a demotion there would leave it
  delivered and invisible;
- terminal entries are pruned when the next turn starts — the one point at which no
  submitter can still be mid-read.

A demoted steer is reported to the panel (`demoted: true` → a notice), so the
user who pressed "Steer" is not left to infer the mode change from a card
appearing.

Cancel and edit are **server-authoritative**: the card changes only once the
server has confirmed, never optimistically. A drain can dequeue an entry between
render and click, and an optimistic update would then show the text as cancelled
while the turn it started is already running. While a mutation is in flight the
card's controls are disabled (`QueueStack`'s `pendingIds`), so a second click
cannot fire a duplicate that races the first and 404s.

Confirmation arrives by **two independent paths** — the HTTP response and the
`chat.side_queue` frame — both dispatching the same replay-safe `sseSideQueue`
reducer, so losing either one cannot desynchronise the panel. A `cancel` releases
its text through `SideState.releasedText`, stashed in the reducer (which both
paths funnel through) and drained + cleared by the panel, so the release happens
exactly once whichever path lands first.

A head-insert broadcast carries `front: true` (a requeued steer, a failed drain's
entry) and the reducer prepends it — appending would show a different next
question than the backend will actually run.

Released text is **merged** into the composer, never chosen over it: a cancelled
entry's text and an in-progress draft are both typed work, and the released text
has no other home (its card or its request is already gone), so neither may be
the one discarded. The same rule covers a rejected submit's text.

Drain: `_run_side_turn`'s `finally` releases the session and then calls
`_drain_side_queue`, which pops one entry and dispatches it. The drain is
identity-checked on `run_id`, so a task belonging to a superseded run cannot
dispatch onto a sidecar that a close/reopen replaced. The queue path in
`api_side_turn` kicks the drain itself when the turn finished during the steer
attempt — the `finally` has already run by then, so the entry would otherwise sit
forever.

Bound: `MAX_SIDE_QUEUE` (20). The sidecar lives in memory on the parent slot, so
an unbounded queue is a client-driven memory sink; past the bound the endpoint
returns 429 and the pressure stays visible to the user.

Placement of a steer bubble: the terminal `chat.side_result` frame carries the
WHOLE turn's text and replaces the last assistant row, so a steer bubble is
inserted ABOVE the streaming answer rather than appended after it. Appending
would strand the reply and make the terminal frame concatenate the full text a
second time.

## Lifecycle

```
 open ──→ turn ──→ turn ──→ ... ──→ close
  │         │         │                │
  │ SideState created │                │ slot._side = None
  │ (open=True)       │                │ side session destroyed
  │                   │
  │        background task spawns,
  │        broadcasts chat.side_result chunks
```

| Endpoint | Method | Path | Effect |
|----------|--------|------|--------|
| open | POST | `/api/chat/slots/{slot}/side/open` | Initialise sidecar (idempotent) |
| turn | POST | `/api/chat/slots/{slot}/side/turn` | Submit question; starts a turn, steers the running one, or queues |
| queue cancel | DELETE | `/api/chat/slots/{slot}/side/queue/{queue_id}` | Drop a queued entry; echoes its text back for the composer |
| queue edit | PATCH | `/api/chat/slots/{slot}/side/queue/{queue_id}` | Rewrite a queued entry in place |
| close | POST | `/api/chat/slots/{slot}/side/close` | Drop buffer + queue + destroy LLM session |

## Wire Protocol

Event name: `chat.side_result`  
Kind field: `"side"` (translated from upstream `"btw"`)

Payload shape (broadcast per chunk and per final response):

```json
{
  "type": "chat.side_result",
  "data": {
    "slot": "<parent-slot-key>",
    "run_id": "<hex-uuid>",
    "role": "user" | "assistant",
    "content": "<text>",
    "kind": "side",
    "ts": <unix-float | null>,
    "is_error": false
  }
}
```

Run-ID isolation: the frontend routes `chat.side_result` frames to
`chatSlice.slotSide` via a dedicated reducer (`sseSideResult`). The main
chat assembler never sees these frames — isolation is structural (separate
event type → separate reducer → separate redux slice), not filter-based.

## Backend Modules

### `dashboard/ws.py` — `broadcast_side_queue`

Emits `chat.side_queue` frames — `{slot, action, queue_id, content?, depth, ts}`
where `action` is `push` | `edit` | `cancel` | `drain`. Held apart from
`chat.side_result` so a queue mutation never enters the transcript reducer, and
apart from the main chat's `queue_push` so a side entry can never be mistaken for
a parent-slot turn.

### `dashboard/side_state.py`

`SideState` dataclass: `open`, `messages`, `last_run_id`, `created_at`,
`is_complete`, `queue`, `steers`.
Helpers: `append_user` (with a `steer` marker), `append_assistant`, `clear`,
`queue_append` / `queue_insert_front` / `queue_pop` / `queue_remove` /
`queue_edit`, and the ledger's `steer_register` / `steer_state` /
`steer_mark` / `steer_pending` / `steer_settle` / `steer_prune_terminal`.

### `dashboard/steer_settle.py`

`settle_consumed_steers(pending, snapshot)` — pure, and shared with the main chat
(`chat_runner._settle_consumed_steers` delegates to it). Matches by EQUALITY and
is count-aware: containment would false-positive a short steer against a longer
one, and a falsely-settled steer is never requeued, so the question is lost.

### `dashboard/side_prompts.py`

Two prompt constants lifted from the upstream protocol:

- `SIDE_BOUNDARY_PROMPT` — establishes ephemeral context and the read-only
  tool boundary the `READ_ONLY` policy enforces, in the footer's words:
  lookups work here (reading files, searching, fetching pages run without
  asking), changes don't (file writes, modifying commands and MCP tools are
  refused), and the user is pointed to the main chat for action. The prompt and
  the policy say the same thing on purpose — a prompt that forbids every tool
  makes a compliant model redirect a read-backed question instead of reading;
  one that permits more than the gate makes it claim a tool is unconfigured.
- `SIDE_DEVELOPER_INSTRUCTIONS` — marks the main-thread/side-thread boundary.
- `build_side_system_prompt()` — concatenates both into the first-turn envelope.

### `dashboard/side_context.py`

- `build_side_message(slot, question, is_first_turn=...)` — with
  `is_first_turn` set, the full envelope: developer instructions + parent
  snapshot + side history + boundary prompt + question; otherwise the bare
  question (the session retains its framing). `_run_side_turn` sets it for
  the sidecar's first turn and for any turn whose acquisition cold-started
  the session (`get_or_create` reported a new, non-resumed session — a rebind
  or an eviction), so a fresh process is never handed a bare question.
- `_format_parent_snapshot(slot)` — renders parent user/assistant turns as
  read-only text block (max 32K chars, 500 chars/line truncation).
- `_format_side_history(slot)` — renders prior side turns for session
  cold-start recovery.

### `dashboard/handlers/side.py`

Three aiohttp handlers + `_run_side_turn` background driver.
`_run_side_turn` resolves the slot's agent, publishes the derived read-only spec
for it (below), acquires an isolated session via `state.sessions.get_or_create`
**bound to that derived agent**, streams with `ToolApprovalPolicy.READ_ONLY`,
broadcasts chunks over `broadcast_side_result`, and appends the final assembled
text to `slot._side.messages`.

**The derived read-only spec is what makes the guarantee hold.** kiro-cli
approves a tool on the agent's `allowedTools` (and an MCP server's
`autoApprove`, a `toolsSettings` `allowed*` / `trusted*` grant, a KAS
`permissions` rule, the `mcp.json` servers `includeMcpJson` pulls in) itself
and raises no permission request, so under the parent agent's own spec the gate
below would never see the user's main-chat grants — the shipped `kirocrew` spec
pre-authorizes `@kirocrew-cron/cron_remove_all` and `@kirocrew-core` wholesale.
So every side session runs as `<agent>--readonly`: the resolved agent's spec
with every backend-side grant emptied (`allowedTools: []`, no
`mcpServers.*.autoApprove`, no `toolsSettings.*.allowed*`/`trusted*`/`auto*`
— `shell.autoAllowReadonly` included — `includeMcpJson: false`,
`autoAllowReadonly: false`, an empty KAS `permissions`) and the lifecycle
`hooks` removed (`agentSpawn`/`userPromptSubmit`/`preToolUse`/`postToolUse`/
`stop` are shell commands the backend runs unprompted, some fed model-controlled
input; the host's SEL audit records every side-turn decision, so the shipped
audit hook is not needed here), with everything else kept (`tools`,
`mcpServers`, `resources`, prompt, model), derived by
`dashboard/side_readonly_spec.derive_readonly_spec` and published by
`publish_readonly_spec` into the user-level kiro agent registry
(`~/.kiro/agents/<agent>--readonly.json` — the only place kiro-cli loads agents
from besides the user's own checkout; see `config.md` § `kiro_agents_dir`). A
PROJECT-scope base is named after its source, `<agent>--readonly-<8 hex of the
project path>`, so two checkouts declaring the same agent never contend for one
file. Derived from the live base spec on every turn — project scope first, the
way kiro-cli resolves `--agent` — off the event loop, written atomically and
only when the content changed; a runtime resource, never hand-edited. The
file's `description` starts with the owner marker `Kiro Crew derived read-only
spec`, and the derived name must resolve to that file and nothing else:
publication refuses a project-scope spec that declares or is named like the
derived agent (kiro-cli would load it first), a second user-scope spec
declaring the name, and a file at the derived path without the marker (a
user's own agent, or a symlink) — none of them is rewritten. Every tool call on
a side turn that kiro-cli does not trust natively therefore raises a permission
request, and the gate judges all of them. kiro-cli trusts `fs_read` natively
(observed on a live pod: an `fs_read` ran with no permission request and no host
decision row, while `pwd` under `execute_bash` raised one the gate approved); a
native read runs outside the gate, and the guarantee holds because it is a
read. FAIL CLOSED: a turn whose spec cannot be derived or published is refused
with a coded error (`ReadOnlySpecError.code`: `unsafe_name`,
`base_spec_missing`, `base_spec_unreadable`, `derived_name_shadowed`,
`derived_path_foreign`, `spec_write_failed`), logged and shown in the panel; it
never runs under the base agent.

**The allowance is a harness capability, granted by positive membership.**
Whether a side turn may execute read-only tools at all is
`ACP_BACKENDS_SIDE_READONLY` (`agent_sdk/backends.py`, harness-parity H6) —
kiro-cli only, because the derived spec is a kiro-cli agent-spec mechanism and
another harness has its own pre-approval surface (claude-agent-acp
`permissions.allow` / `bypassPermissions`, KAS `permissions` rules from its
own store) that neither the derived spec nor the host gate can see: a call it
pre-approves would run with no READ_ONLY decision and no SEL row. Off the set,
or with the harness unknown because the config never loaded, `_run_side_turn`
keeps the pre-allowance posture — the base agent under `REJECT_ALL`, no
derived spec — and the prompt (`SIDE_BOUNDARY_PROMPT_NO_TOOLS`), the
empty-output fallback and the footer
(`pages.chat.sideChat.context_only_tools_unavailable_backend`, chosen by
`SideChat.tsx` from `agent.acp_backend`) say tools are unavailable there. Never
`not is_claude_backend`: a harness joins by demonstrating that every tool call
it serves reaches `session/request_permission` under the derived spec.

**One session per sidecar generation, rebound not trusted.** The side session
key is `side:<slot>:<gen>`, `gen` being the sidecar's own generation
(`SideState.gen`): a close+reopen makes a new sidecar and a new key, so a turn
still finishing on the old one can only destroy, acquire or release ITS
session, never the replacement's, and a task that finds the sidecar replaced
when its off-loop derivation returns drops out before touching any session.
`sessions.get_or_create` reuses a live session for a key whatever agent or cwd
the call names, and kiro-cli read the spec at spawn, so `SideState.binding`
records the `(derived agent, cwd, spec digest)` the live session was created
under; when a turn's binding differs — the slot's agent or project changed, or
the base spec changed so the derived content did — or a live session exists
that no binding vouches for, `_run_side_turn` destroys the session and the
acquisition cold-starts under the current derived spec, sending that turn the
full envelope rather than a bare follow-up; a close that lands while the
destroy is in flight ends the turn before it acquires anything, and one that
lands while `get_or_create` is in flight has the turn destroy the session it
was handed, since no sidecar owns it. The turn reads the slot's project and
agent ONCE, before its first await: the shadow scan, the spawn cwd (where
kiro-cli resolves `--agent` first) and the recorded binding all name that one
project, so a project change landing mid-turn cannot have the check run in the
old project and the spawn happen in the new one; the change is picked up by the
next turn, whose binding differs. Closing the panel destroys the closing
generation's session outright.

`READ_ONLY` is the dashboard Reads mode's semantics with reject as the
fallback, because the side chat has no approval card. The gate is the
gateway's one live `HookManager` — `state.context_builder.hooks`, the same
object the main chat consults and the one Settings > Security hot-reloads
(`handlers/security._reload_live_hooks`), so opt-outs and deny patterns apply
here too and a deny added mid-turn binds an in-flight side turn. No per-turn
manager is built (that would re-read the keystone `denied_commands.json` on
the event loop and freeze the opt-out state at turn start). A host with no
context builder passes `hooks=None`, and `READ_ONLY` then rejects every tool
call (`read_only_policy_no_hooks`). The gate runs its deny floor and
governance first; then only the
read-only classifier may auto-approve, and its verdict carries
`ToolHookResult.read_only`. The gate is asked `classifier_only`, so the
operator's `auto_approve_tools` globs and the app-own-server rule are skipped
rather than honoured (they vouch for the caller, not for the call's effect),
and an auto-approve without the tag is rejected. A grant that shadows a read
therefore still gets the read approved; a grant that shadows a write approves
nothing. Every other call is rejected before any interactive callback could
widen the policy, and a missing gate rejects everything.

Because no approver stands behind the verdict, `classifier_only` accepts only
HOST-TRUSTED proof of read-only. Exactly two sources qualify:

- the shell command recovered from the tool call's cached params, judged by
  `is_read_only_bash` (the deny-by-default bash classifier);
- a built-in tool the host knows to be read-only —
  `hooks._HOST_READ_ONLY_BUILTIN_TOOLS` (`fs_read`, `glob`, `grep`,
  `web_fetch`, `web_search`), matched on the non-model-authored
  `_meta.kiro.toolName` identity with no `_meta.kiro.mcpServerName` behind it.

The agent-influenced inputs — the ACP `kind` field (passed through verbatim
from the agent) and the model-authored title — may narrow but never prove: a
non-read kind refuses even a host-known read tool, and `kind="read"` on an
unknown or mutating tool, a read-looking title on a kindless call, and every
MCP-served tool (the permission event carries no host-trusted read-only marker
for one) are rejected. The interactive Reads mode (`HOOK_BASED`) is unchanged
and keeps its `{read, fetch}` ACP-kind allow-list and title fallback, because a
card stands behind it.

Governance identity: the gate is called with the side session's own key,
`side:<slot>:<gen>`, which `sel._infer_source` classifies as the `dashboard`
surface. A profile bound to `surface: dashboard` therefore governs side turns
exactly as it governs the parent slot, while the key itself keeps the side
session's SEL rows and ACP session apart from the parent's.

**Residual: a concurrent same-UID writer into `<project>/.kiro/agents`.**
`publish_readonly_spec` refuses every STATIC shadow spec — a project-scope file
named or declaring the derived agent, a second user-scope claimant, a foreign
file at the derived path — before it publishes. What remains is a window
between that check and kiro-cli's spawn in which a writer running as the same
user drops a grant-bearing `<agent>--readonly.json` into the project's
`.kiro/agents`, which kiro-cli searches first. That writer needs write access
to the checkout as the operator's own UID, and with it the same writer can
already shadow the BASE agent under kiro-cli's project-agent precedence — the
main chat has always run with that exposure. It is therefore not a new class
this surface opens, and it is accepted as a same-UID residual rather than
closed with a lock the base agent does not have either.

**Residual: `git` under `READ_ONLY` runs repository-configured programs.**
`is_read_only_bash` approves `git status`, `log`, `diff`, `show`, `branch`,
`tag`, `remote`, `rev-parse`, `describe`, `ls-files`, `ls-tree`, `cat-file`
and `blame` (and refuses their output-file, `--ext-diff`/`--textconv` and
`--filters` spellings). Git itself may run a program named in the repository's
own `.git/config` — `core.fsmonitor`, a diff or textconv driver — and a hostile
value there would execute under a "read". A side turn runs git as the same user,
against the same `.git/config`, that the main chat's Reads mode runs it under;
`.git/config` is not versioned, so a checkout cannot ship the value — only
someone already writing as this account can plant it, at which point the
account is compromised without any help from the side chat. Accepted as a
same-UID residual, not a gate defect.

**Decision: `web_fetch` and `web_search` stay in the side-chat read-only set.**
`_HOST_READ_ONLY_BUILTIN_TOOLS` includes both, so a side turn can fetch a page
or run a search without asking, on a surface with no approver, and injected
page content could then steer a later fetch (an egress channel). The tradeoff
is recorded and accepted: the shipped default agent spec
(`config/defaults.json` `allowedTools`) already auto-approves both in the main
chat, so the side chat adds no egress surface the main chat lacks. Mitigations
in force: the `READ_ONLY` gate refuses every write and every MCP tool, so a
fetched instruction can read and fetch but change nothing; side turns are not
saved to the conversation; every decision leaves a SEL row keyed to the side
session; and the derived `<agent>--readonly` spec strips every pre-approval
channel, so nothing on the surface runs unjudged. An operator who wants no
egress from the side chat denies the two tools with an `auto_deny_tools` rule
or a governance profile — both bind on this surface exactly as on the main
chat.

### `dashboard/ws.py` — `broadcast_side_result`

Module-level helper emitting `chat.side_result` frames to all WS clients.
Deliberately separate from `broadcast_ws` main-channel events.

## Frontend Modules

### `ActivityViewer.tsx`

5th tab: `{key: 'side', label: i18nT('pages.chat.activityViewer.side'), icon:
MessageCircleQuestionMark}`. Renders `<SideChat slot={slot} />` when active.

### `SideChat.tsx`

Reads from `state.chat.slotSide[slot]`. Calls `api.sideOpen` on mount,
`api.sideTurn` on submit. Local optimistic buffer for pre-redux rendering — for
a turn this submit STARTS only: a steer's bubble must land above the streaming
answer and a queued one is a card, so both are placed by the server frame.
While a turn is in flight the composer stays editable and swaps its send button
for the shared `BusySendButton`; queued entries render as `QueueStack` cards whose
cancel and edit wait for the server's own frame before changing what the user
sees. A persistent helper beneath the composer
(`pages.chat.sideChat.context_only_tools_unavailable`) states that Side Chat is
read-only: lookups work here, but changes don't, and action belongs in the main
chat. That is a guarantee, not a description, and the derived `<agent>--readonly`
spec is what makes it one: with `allowedTools: []` no tool call bypasses the
`READ_ONLY` gate, and the gate refuses everything it cannot prove read-only.
The backend's empty-output fallback in `_run_side_turn` and the model-facing
`SIDE_BOUNDARY_PROMPT` use the same vocabulary. Unlike an empty-state note, the
helper remains visible after messages exist.

The composer's DRAFT behaviour is not owned here. It comes from the chat SDK's
`app-sdk/useComposerDraft`, which this surface was the first consumer of
(`app-sdk/ChatEmbed.tsx` the second, wired to its bare `<input>` via the same
`submitOnEnter`/`isComposing` without attaching `textareaRef` — no auto-grow
box to size), and which owns four invariants this file must not re-derive:

- A follow-up pick edits the draft, and the picked set is read back OFF the draft
  rather than stored beside it. The draft is what gets submitted, so it is the
  only source that cannot disagree with what the user sends.
- Text the server hands back (a cancelled queue entry, a rejected submit, a
  failed edit) is APPENDED to the draft, never substituted for it — via the
  host's single `utils/chatDrafts.mergeIntoDraft`, which `chatSlice`'s own
  release path already uses.
- An Enter that commits an IME candidate is not a submit. This surface's own
  handler predated the shared hook and lacked the guard, so a Chinese/Japanese/
  Korean candidate confirmed with Enter submitted the partial text with nothing
  left to recover. Declining the submit does not release the key: the guard
  consumes it (`useImeGuard`'s `claimEnter`), because the browser's default for an
  unclaimed Enter is to put a line break in the draft. Recovery from a composition
  abandoned without a `compositionend` ships with the tracking rather than with the
  caller -- `ime.bindComposition()` is the only composition binding the hook
  exposes, and it carries the blur reset, because a latched guard now declines
  Enter silently instead of visibly.
- The submit size limit (`MAX_QUESTION_BYTES`) is measured in UTF-8 bytes, not
  code units. The hook only reports whether the limit is exceeded; this file
  still owns the refusal and its wording.

The hook is uncontrolled here (it holds the draft), but it also accepts a
caller-owned draft via `draft` + `onDraftChange`, and its `submitOnEnter` /
`isComposing` are generic over the element -- the shape the remaining consumers
need, so migrating them does not change its signature. It is deliberately NOT
exported from `app-sdk/index.ts`: that barrel is re-exported through the vendor
stub to third-party apps, and the contract should not be frozen for them until
the main composer -- with configurable send keys and per-slot persisted drafts --
has exercised it. In-tree surfaces import it from `app-sdk/useComposerDraft`
by name.

`website/src/test/useComposerDraft.test.tsx` holds the behavioural tests, two of
them rendered inside `StrictMode` (a toggle has to remember what it wrote, so
the write must happen outside the state updater -- an impure updater loses the
memo on the second pass and eats the user's punctuation).
`website/src/test/SideChat.imeEnter.test.tsx` drives this file's real textarea,
because the IME defect was in the WIRING and a hook test cannot see it. A
source-level guard fails if this file re-grows a local copy of any of them.

### `chatSlice.ts` — Side State

- `slotSide: Record<string, SideState>` on ChatState, each with `messages` and
  `queue`.
- `sseSideResult` reducer: user frames append, assistant frames accumulate
  (delta-append within same run_id), error frames always start new entry, and a
  `steer` user frame is spliced in ABOVE a streaming assistant row of the same
  run.
- `sseSideQueue` reducer: `push` appends (replay-safe — a redelivered id updates
  in place), `edit` rewrites, `cancel`/`drain` remove. Never resurrects a closed
  side.
- `sideClose` action drops per-slot side state.
- Cleaned up in `deleteSlot.fulfilled`.

### `useWebSocket.ts`

`case 'chat.side_result':` dispatches `sseSideResult` and
`case 'chat.side_queue':` dispatches `sseSideQueue` — two lines alongside the
existing subagent/tool dispatch cases.

## Security & Isolation

| Concern | Mitigation |
|---------|-----------|
| Tool execution | System prompt prohibition + READ_ONLY approval policy: only the read-only classifier's verdict approves, and under `classifier_only` that verdict rests on host-trusted facts alone (`is_read_only_bash` on the recovered shell command, or a `_HOST_READ_ONLY_BUILTIN_TOOLS` name on the non-model-authored `_meta.kiro.toolName` with no MCP server); the agent-influenced ACP `kind` and title may narrow but never prove; operator `auto_approve_tools` globs and app-own-server grants are skipped and an unclassified auto-approve is rejected |
| Governance identity | The gate runs under the side session's own key `side:<slot>`, which `sel._infer_source` classifies as the `dashboard` surface, so a profile bound to `surface: dashboard` binds side turns exactly as it binds the parent slot (it fell through to the `slack` fallback before) |
| Memory pollution | No calls to memory/learn/save; sidecar never serialised |
| Context leak to main | `build_message` byte-frozen; side uses separate module |
| Slot visibility | No new `_ChatSlot` created; sidebar doesn't show phantom entries |
| App isolation | `_check_slot_ownership` mirrors main chat ownership checks |
| Session cleanup | `api_side_close` destroys kiro-cli session files |

## Testing

Backend invariants are covered by `test/test_side.py`:

| Invariant | Test |
|-----------|------|
| Memory isolation | `test_memory_isolation_byte_equal_after_round_trip` |
| Same-session reuse | `test_side_path_never_creates_a_new_slot` |
| Non-blocking stream | `test_side_turn_returns_before_run_finishes` |
| Channel separation | `test_side_run_id_never_leaks_to_main_channels` |
| Tool-rejection fallback | `test_empty_llm_output_produces_visible_fallback` |
| READ_ONLY honours the classifier, never a grant | `test_read_only_policy_refuses_a_write_the_config_grant_matches`, `test_read_only_policy_refuses_an_app_own_server_grant`, `test_read_only_policy_classifies_a_read_the_grant_also_matches`, `test_read_only_policy_does_not_trust_a_read_kind_alone` (`test/test_llm_helpers_tool_gate.py`) |
| READ_ONLY proof is host-trusted only | `test_read_only_policy_rejects_a_read_kind_on_a_mutating_tool`, `test_read_only_policy_rejects_a_read_kind_with_no_host_identity`, `test_read_only_policy_rejects_a_host_known_name_without_trusted_provenance`, `test_read_only_policy_rejects_a_read_looking_title_alone`, `test_read_only_policy_rejects_an_mcp_tool_with_a_read_kind`, `test_read_only_policy_rejects_a_host_known_read_tool_under_a_non_read_kind`, `test_read_only_policy_approves_a_host_known_read_tool`, `test_read_only_policy_approves_a_read_only_shell_command`, `test_hook_based_policy_still_approves_a_read_kind_tool` (`test/test_llm_helpers_tool_gate.py`); `TestClassifierOnlyHostTrustedProof` (`test/test_hooks.py`) |
| Dashboard-bound profile governs a side turn | `test_dashboard_bound_profile_governs_a_side_turn` (`test/test_side.py`), `test_side_key_binds_the_dashboard_surface` (`test/test_governance_profiles.py`), `TestInferSource` (`test/test_sel.py`) |

Busy-send invariants live in `test/test_side_steer_queue.py`:

| Invariant | Test |
|-----------|------|
| Steer reaches the live turn | `test_in_flight_steer_injects_into_the_running_turn` |
| Unavailable steer never drops text | `test_steer_unavailable_falls_through_to_the_queue` |
| Queue mode leaves the session alone | `test_queue_mode_defers_even_when_steer_is_available` |
| FIFO, one entry per turn | `test_queue_drains_fifo_one_entry_per_turn` |
| Cancel / edit | `test_queue_cancel_removes_the_entry_and_echoes_content`, `test_queue_edit_rewrites_in_place_preserving_order` |
| Bounded queue | `test_queue_refuses_past_its_bound` |
| No stranding past the finally | `test_entry_queued_after_completion_is_not_stranded` |
| Steer identity binding | `test_a_steer_that_lands_after_its_run_ended_is_queued_not_claimed`, `test_a_steer_cannot_land_on_a_sidecar_that_was_replaced` |
| Unproven delivery is recovered | `test_an_unconsumed_steer_becomes_a_queue_card_instead_of_vanishing` |
| A proven delivery is not duplicated | `test_a_consumed_steer_is_settled_and_not_requeued` |
| A failed drain keeps the text | `test_a_failed_drain_puts_the_entry_back_instead_of_dropping_it` |
| Close wins over a late drain | `test_close_drops_the_queue_and_a_late_drain_cannot_resurrect_it`, `test_a_stale_task_cannot_drain_a_newer_sides_queue` |

The settlement rules themselves are in `test/test_steer_settle.py` (equality not
containment, count-awareness, settle-all on an unusable echo), and
`test/test_llm_helpers_steer_echo.py` covers the REAL `stream_and_collect`
dispatch — every steering caller fakes that helper, so without it the hook could
be dead at runtime while all of them stayed green.

Frontend invariants are covered in KiroCrewWebsite under `src/test/`:
`SideChat.close.test.tsx`, `SideChat.multiturn.test.tsx`,
`SideChat.refresh.test.tsx`, `SideChat.thinking.test.tsx`,
`SideChat.steerQueue.test.tsx`, `SideSlashCommand.test.tsx`,
`SideSlashCommand.steer.test.tsx` (command interception wins over mid-turn
steer routing), and the `sseSideResult` block in `chatSlice.test.ts`.

Nothing enforces invariant 2 (main path byte-frozen) mechanically. It is a
review-time rule, held by the backend and frontend suites above plus the
`_side` sidecar's own isolation from `context.build_message`: a side symbol
reaching the main path shows up as a `test/test_side.py` failure, not as a
structural check. Making the freeze a real invariant needs a test; until one
exists this file does not claim one.

## Design

- Context injection: sidecar storage on the parent ``_ChatSlot``
  (per-slot ``_side: SideState | None``) — keeps side messages reachable
  to the side handler without touching the main agent's context builder.
- Build strategy: native KiroCrew implementation reusing only the
  upstream wire format for the ``chat.side_result`` event.
- Memory mode: side conversations are born ephemeral and never write to
  vector store, learn store, KiroCrew session JSONL, or the consolidation
  pipeline. Tool calls run under `ToolApprovalPolicy.READ_ONLY`: only a call
  the hook gate's read-only classifier proves read-only executes, and every
  other call — `learn_add` and any other write tool included — is rejected.
  `side:` prefix is registered in `session._STATELESS_PREFIXES`, so the
  isolated kiro-cli ACP session is never resumed across gateway restarts;
  its on-disk transcript at `~/.kiro/sessions/cli/<sid>.jsonl` exists
  only while the side is open and is destroyed by `/side close` via
  `state.sessions.destroy(side_key)`.
