// Shared types for chat sessions and the notices they raise: the slot row
// (`ChatSlot`) with its turn identity and the `chat_done` payload, its todo
// list, MCP report and channel links; sidebar folders, tags and board lanes;
// transcript messages and the slot detail read; subagent and tool activity;
// content blocks; notifications and their channels; approvals; history session
// rows. Consumers import them from `types/index.ts`.
import type { WebAppMetadata } from './index.artifacts'
import type { SourceProviderId } from './index.sources'

export interface TodoTask {
  id: string
  text: string
  /** kiro-cli's todo model is a plain boolean — there is no in-progress state. */
  completed: boolean
  /** True while a person's click holds this row's state and the agent has not
   * yet confirmed it in its own list. Absent on rows the agent itself set. */
  person?: boolean
}

/**
 * The agent's own TODO list for a slot, mirrored from the `todo_list` tool.
 *
 * `completed`/`total`/`current` are computed server-side so the pill's "N of M"
 * label can never drift from the list it summarises. `current` is the first
 * not-completed task and is a DERIVATION — the agent does not report a current
 * task. Absent (`null`/`undefined`) means the agent never used its todo tool,
 * which renders as no pill; a present list with zero tasks means it cleared the
 * list, which is a different thing.
 */
export interface TodoList {
  description: string
  tasks: TodoTask[]
  completed: number
  total: number
  current: string
}

/**
 * What ONE agent session's MCP servers reported while starting.
 *
 * Distinct from every other MCP payload in the dashboard: `/api/mcp/active`
 * reads an agent spec off disk and `/api/mcp/probe` records whether the gateway
 * itself can start a server. Both answer a question about the host. This is the
 * only one that answers "what did THIS session actually mount".
 *
 * Two properties callers must respect:
 * - A name absent from every bucket means *no report yet*, never *not mounted*:
 *   the backend's init drain is time bounded and a late frame still arrives.
 * - The buckets are a SUPERSET of `configured`, because the backend also starts
 *   the agent spec's own servers, not just the ones Kiro Crew injects.
 */
export interface McpSessionReport {
  /** Server names Kiro Crew put on the wire for this session. */
  configured: string[]
  /**
   * Agent-spec `@server` tool refs that named no server this session receives.
   *
   * A different claim from every bucket below: those say what a *configured*
   * server reported, this says the spec asked for one that was never
   * configured — so it has no row here to be missing from. Optional because a
   * gateway from before the guard shipped sends no such key.
   */
  unresolved_refs?: string[]
  /**
   * Servers that started but gave this session no tools — usually a tool name
   * that clashes with another server's, so the backend kept only one. Optional
   * because an older gateway sends no such key.
   */
  no_tools?: string[]
  /** How many more such servers there were than `no_tools` lists (bucket cap). */
  no_tools_omitted?: number
  /** Reported initialized. */
  ready: string[]
  /** Reported a startup failure. */
  failed: string[]
  /** Asked for authorization and has not reported since. */
  awaiting_auth: string[]
  /** Server name -> its redacted failure reason, when one was reported. */
  failures: Record<string, string>
  /**
   * Failed servers whose reason reads as a credential they did not have when
   * they started: a new session loads them once the credential exists.
   * Optional because an older gateway sends no such key.
   */
  restart_to_load?: string[]
}

export interface SessionLink {
  channel: string
  label: string
  /** Redacted display tail of the conversation id — never the id, never a key. */
  target: string
  /**
   * Opaque identity of the whole binding (channel, full conversation id, thread),
   * minted server-side. An unlink names it, and the server refuses a row whose
   * binding has since been replaced — `target` alone cannot tell a Slack thread
   * from its same-channel replacement. Optional for the same reason as `paused`:
   * a cached `slots` payload from before this field shipped has none, and a
   * row sent without it is refused as stale rather than unlinking anything.
   */
  binding?: string
  /**
   * `origin` — the conversation the session started on.
   * `out`    — dashboard replies are mirrored there (one-way, from `!link`).
   * `both`   — a session-RESUME binding from an in-channel `!sessions` pick:
   *            replies go there AND messages from there land in this session.
   *
   * Provenance only. It does NOT decide whether a row is operable — an `origin`
   * row carries a disconnect control like any other (see `paused`). One channel
   * can also carry TWO rows at once (born there AND mirrored there), so
   * `channel` alone does not identify a row; pair it with origin-ness.
   */
  direction: 'origin' | 'out' | 'both'
  /**
   * Messages sent in that conversation land in THIS session. The server's
   * statement of inbound routing, per row, because it is not readable from the
   * other fields: a Slack thread is `out` (Slack routes replies through its own
   * thread index, not the mirror's inbound marker) yet a reply there resumes
   * this session; a `both` mirror routes inbound by that marker; a one-way
   * `out` mirror only receives replies; the conversation a session was born in
   * is where its turns come from. What a sever destroys differs between a row
   * that drives the session and one that does not, so the menu's sub-lines
   * read this rather than inferring it from `direction` or the channel name —
   * the inference is wrong for a paused Slack row. Optional for the same
   * reason as `binding`: a cached `slots` payload from before this field
   * shipped has none, and such a row reads as not driving until the next push.
   */
  drives_session?: boolean
  live: boolean
  /**
   * The user disconnected this channel: turn output stops flowing there, but the
   * binding is retained so a reply in that conversation resumes the same session.
   * Distinct from `live`, which reports whether the transport *can* send at all —
   * a disconnected channel on a healthy transport is still `live`.
   *
   * Set on every row including `origin`, because the conversation a session was
   * born in can be disconnected too. `direction` records provenance only; it does
   * not decide whether a row has a control.
   *
   * Optional because a browser holding a `slots` payload cached from before this
   * field shipped has links without it; absent reads as connected.
   */
  paused?: boolean
}

export interface ConfiguredChannelTarget {
  channel_type: string
  target_id: string
  label: string
  available: boolean
  unavailable_reason: string
}

/** Turn identity is monotonic only within one gateway process generation. */
export interface TurnIdentityFields {
  turn?: number
  turn_gen?: string
}

/** Terminal WebSocket payload; identity is optional for older gateways. */
export interface ChatDonePayload extends TurnIdentityFields {
  slot: string
  continuing?: boolean
  needs_input?: boolean
}

export interface ChatSlot extends TurnIdentityFields {
  /** Which namespace `agent` was chosen in: a configured member, a shared
   *  provider template, or "" when the choice was made by name alone or came
   *  back from history. Display provenance for the picker's selected row; the
   *  backend never reads it as authority. */
  agent_kind?: 'member' | 'template' | ''
  /** The agent that will actually answer, when it is NOT the requested `agent`;
   *  "" / absent means nothing to report. The backend stores `agent` verbatim
   *  (the user's intent) and reports the divergence here instead of rewriting it,
   *  and it reports "" rather than guessing whenever resolution is unsettled — so
   *  a consumer must treat absent as "no news", never as a mismatch. */
  effective_agent?: string
  /** The backend's verdict on whether the live session can run `model`:
   *  `true` it cannot (the spawn withheld the pin and the session is on the
   *  backend default), `false` it can, `null`/absent NOT KNOWN YET — no session
   *  has advertised a comparable list for this pin.
   *
   *  Consumers must fail open on the unknown state (`displayModel` does): it is
   *  the absence of an answer, never a denial. DISPLAY only — the pin is
   *  deliberately kept when withheld, so this must not drive a write. */
  model_withheld?: boolean | null
  /** Whether this session's turns ask Jev which model tier to run on -- the chat
   *  picker's `Auto (Jev)` entry (`lib/jevRoute.ts`, `decisions/points/model_route.py`).
   *
   *  Shipped on every slot, so "the owner picked a model by hand" is a positive
   *  value rather than an absent key. Unlike `model_withheld` and `served_model`
   *  this IS the owner's own choice, so it drives the picker's highlight: a routed
   *  slot stores `model: 'auto'`, and highlighting the Auto row would name a
   *  behaviour the session does not have. */
  jev_route?: boolean
  /** The model id the live session actually resolved to; `''`/absent when not
   *  known. A slot with no pin — or one whose pin was withheld — runs on the
   *  backend's own choice, which the pin cannot name, so this is what lets a
   *  chip say the model instead of `auto`. DISPLAY only, like
   *  `model_withheld`: it describes the session, so it must not drive a
   *  write. */
  served_model?: string
  /** Remote-execution binding. `executor` is "local" for an ordinary session and
   *  "remote" for one whose turns run on a connected crew; `instance_id` names
   *  that crew. The backend ships BOTH on every slot so "runs locally" is a
   *  positive value rather than an absent key — otherwise an older gateway's
   *  payload would read as local for a session that is not. The binding's third
   *  field, the peer's own slot key, stays server-side: it is meaningful only
   *  inside a request routed back through that instance. */
  executor?: 'local' | 'remote'
  instance_id?: string
  /** The identity the sidebar renders this row under, resolved by the SERVER.
   *
   *  A purely local session is its own key. A remote-bound one — minted on a crew
   *  or adopted from a peer row — is `<instance_id>:<peer_key>`, the identity the
   *  peer row already carried. Preserving it across the bind is what makes the row
   *  the user clicked BECOME the session, rather than a second element appearing
   *  beside it, and it keeps a `data-session-row` selector stable across the adopt.
   *
   *  Absent on an older payload, and absent on a peer row — `sessionRowIdentity`
   *  falls back to `peer_id` + `key` for those. Never parse it to recover the local
   *  slot key: read `key`. */
  row_identity?: string
  /** Provider session identity when it differs from the dashboard slot key. */
  linked_session_key?: string
  /** Prompts held for a later turn on this slot. */
  queue_depth?: number
  key: string; title?: string; messages: number; running: boolean; stopping?: boolean; pending_approval?: boolean; created?: string; last_ts?: string; last_turn_ts?: string; last_message?: string; agent?: string; model?: string; reasoning_effort?: string; mode?: string; surface?: string; workspace?: string; trust?: boolean; trust_scope?: string; trust_reads?: boolean; folder_id?: string; pinned?: boolean; /** Place in the gateway's pinned order, or null when it has none. */ pin_rank?: number | null; /** Revision of the pinned order the rank belongs to. */ pin_rev?: number; /** Place in the gateway's manual order (sort "Manual"), or null when it has none. */ manual_rank?: number | null; /** Revision of the manual order the rank belongs to. */ manual_rev?: number; tags?: string[]; tags_revision?: string; links?: SessionLink[]; slack_linked?: boolean; slack_channel?: string; slack_thread_ts?: string; color_index?: number | null; color_hex?: string | null; memory_mode?: 'persistent' | 'incognito' | 'temporary'; project?: string; forked_from?: string | null; source_links?: { provider: SourceProviderId; number: number; url: string; label?: string; repo?: string; ci?: 'running' | 'passed' | 'failed' | null; ciPending?: string; state?: 'open' | 'draft' | 'merged' | 'closed'; mergeable?: string; mergeStateStatus?: string; kind?: 'change' | 'issue'; identity?: string }[]; source_links_total?: number
  /** Provenance bucket from the backend `SlotOrigin` ("user" | "app" | "cron"
   * | "system"; absent/"" for untagged background slots). The session-pulse
   * survey shows only on a "user" slot, so an imported Slack thread, a
   * task-runner slot, or an app/cron-minted session (which can share the
   * `chat-<n>-<ts>` key shape) never triggers it. */
  origin?: string
  /** The installed app that owns this session, or null/absent for a person's own
   *  chat. Lets the model chip ask for that app's own model setting. */
  app?: string | null
  /** Slot key of the session that asked for this one via the session-control
   * create verb; "" / absent for a person's own tab, a fork, a restore. Durable
   * (written at birth, rehydrated), so it is the one link from a crew member's
   * DM thread to the worker sessions it drives — the Crew Members drawer
   * filters the live slots on it. */
  created_by?: string
  /** True on a Crew Mode root: a root session running a conductor template
   *  (`crew_mode.stamp_rows`). The session list styles and filters on it. */
  crew_mode_root?: boolean
  /** On a session a Crew Mode root dispatched (directly or through another
   *  worker): that root's key. The person's session list leaves such a row out;
   *  the side panel lists it under its root. Absent on every other row. */
  crew_root?: string
  /** The conductor the chat runs in Crew Mode (`crew_mode_agent` on the slot), "" when
   *  it never turned on: it marks the chat as Crew Mode until the gateway moves it onto
   *  the configured `agent.crew_mode_agent`. */
  crew_mode_agent?: string
  /** Whether THIS gateway process stamped `created_by` at mint, rather than
   * rehydrating it from the transcript. `created_by` is restored from a metadata
   * line an agent's file tools can edit, so on its own it is a claim; this says
   * whether it is the gateway's own witness. `_attach_slot_parents` requires it
   * before nesting a row that has no session-tree node yet — a child between
   * `session_create` and its first turn, which has no crew log to fold. */
  lineage_minted?: boolean
  /** A worker tab's process: held (`live`) or stopped by the idle sweep
   * (`released`); the conversation resumes on the next message either way. */
  process?: 'live' | 'released'
  /** The user-owned "mute sessions it opens" rule. When true on a creating
   * session, every session it opens -- and anything those open, down the
   * `created_by` chain -- is muted for attention: no turn-done chime,
   * background-finished toast or unread badge, from the first turn. The
   * creator keeps all of its own signals, and a tool-approval prompt from a
   * muted session still surfaces. Set only by the user (never an agent);
   * durable, so it survives a gateway restart. */
  mutes_opened?: boolean
  /** The user-owned per-row mute of THIS session, set from its own kebab menu.
   * When true: no turn-done chime, background-finished toast or unread badge.
   * A completion that pauses
   * for the user, a terminal error and a tool-approval prompt still surface.
   * Durable, like `pinned`. */
  muted?: boolean
  /** The session tree's parent edge for this slot, attached to every row by
   * `_attach_slot_parents`: `{slot, key}`, or null when this slot has no parent.
   * `slot` is the parent's own citation and `key` names the parent's row IN THIS
   * payload (bare slot key), or null when the parent is not running or sits on a
   * cycle.
   *
   * Distinct from `created_by`, and both are needed to know whether a slot is a
   * root: `created_by` is the birth-time edge, written once, while this one
   * carries the edge an adopt or release moves later. An adopted session has an
   * empty `created_by` and a parent here.
   *
   * `ancestor` is present and true only when `key` names an open ANCESTOR instead of
   * the creator in `slot`, which the backend resolves when the creator itself has
   * closed: the row nests where the run is owned and keeps citing its closed
   * creator. */
  parent?: { slot: string; key: string | null; ancestor?: boolean } | null
  /** Artifact companion binding: slug of the artifact this slot is a companion
   * chat for. Set at slot create and persisted in the history meta line, so the
   * binding survives a gateway restart and a History-page resume. */
  artifact?: string
  /** Metadata for kind="webapp" artifacts (deploy state, architecture, costs). */
  webapp_metadata?: WebAppMetadata
  // Board fields
  has_options?: boolean; options_ts?: string; options?: string[]; pending_approval_info?: PendingApproval | null; last_activity_ts?: string; last_agent_ts?: string; waiting_for_input?: boolean; prompt_preview?: string; subagents_running?: boolean
  /** The sub-agent queued depth the gateway last published for this session —
   * the same value as its newest `subagent_queued` frame. Read only to
   * reconcile `chat.subagentQueued` on a `slots` push; absent from a
   * `slot_patch` and from an older gateway. */
  subagents_queued?: number
  /** An unanswered question card the turn is parked on, so the row would
   * otherwise read "Thinking…" with nothing able to advance it. Narrower than
   * `waiting_for_input` (true of every finished turn, and therefore no signal)
   * and separate from `pending_approval` (a tool gate). */
  needs_input?: boolean
  /** The transcript shows the last turn ending without a reply (trailing error
   * row or unanswered user row) — the state behind the composer's Resume
   * button. Always false while `running`. Lets the sidebar stop rendering a
   * goal-loop session as actively working when it is actually stalled. */
  interrupted?: boolean
  /** A loop wake is opening the running turn and its row is not in the
   * transcript yet (a structured monitor writes it only once the runner
   * accepts the turn). Only ever true while `running`; absent from an older
   * gateway. */
  wake_starting?: boolean
  // Soft-stop state machine
  stop_state?: 'idle' | 'soft_pending' | 'killing'
  /** An automatic context compaction holds the session right now. Not a turn,
   * so `running` stays false while it runs, which is exactly what made it look
   * like a stall worth pressing Stop on (#14841). The composer shows it and
   * the Stop button becomes a warning while it is set. */
  compacting?: boolean
  /** The gateway is preparing an assistant message for this slot outside any
   * turn (a background model call whose reply will land as an ordinary row).
   * Not a turn: `running` stays false, so sends and queueing are unchanged;
   * only the chat's working indicator reads it. Absent from an older gateway. */
  preparing?: boolean
  /** A cooperative Stop was declined moments ago (compaction); the next press
   * escalates to the force stop. Read from the same window the backend uses. */
  stop_declined?: boolean
  /** In-flight `wait` tool sleep, absent when nothing is sleeping. `deadline_ts`
   * is absolute seconds on the BACKEND clock (Date.now() / 1000 territory), so
   * the transcript can count down against it and survive a page reload;
   * `wait_id` is the handle the End-wait button must quote. */
  wait_state?: { wait_id: string; seconds: number; deadline_ts: number } | null
  /** Agent TODO list. Null/absent = the todo tool was never used in this slot. */
  todo?: TodoList | null
  /**
   * What this slot's agent session reported about its own MCP servers.
   *
   * Null/absent means this slot has no session that reported — render that as
   * absence of knowledge, NOT as "no servers". It is deliberately separate from
   * `/api/mcp/active` and `/api/mcp/probe`, which answer questions about the
   * HOST (what an agent spec declares, what the gateway can start) rather than
   * about this session.
   */
  mcp_report?: McpSessionReport | null
}

export interface ChatFolder {
  id: string; name: string; collapsed?: boolean; order: number; parent_id?: string; color?: string; icon?: string; default_agent?: string; default_destination_crew?: string; project_dir?: string; hidden?: boolean; history_count?: number
  /** Epoch seconds the folder was created, written by every folder creator since
   *  the sidebar's `created` sort existed. Absent on a row from before that; such
   *  a row sorts as older than every stamped one. Read only through
   *  `folderComparator('created')`, which mirrors the Python reader. */
  created_at?: number
  /** Tag ids (from the tag vocabulary) copied onto every NEW chat filed into
   *  this folder. Absent = no tags, mirroring the optional `color`. */
  tags?: string[]
  /** Extra steering directories loaded, in addition to the global and project
   *  steering, for every chat whose folder is in this folder's subtree.
   *  ACCUMULATIVE up the parent_id chain (unlike `project_dir`, which is
   *  nearest-wins). Absent = none, mirroring the optional `tags`. */
  steering_dirs?: string[]
  /** Principal that owns the folder: an app's name, `member:<store>` for a
   *  crew member, absent/empty for the person. Folder steering from an
   *  ancestor owned by ANOTHER principal is never delivered to this folder's
   *  chats, so the inherited list filters on it. */
  owner_app?: string
  /** Channel namespace when this folder was created by per-channel session filing (e.g. 'discord'). */
  channel?: string
}

export type AgentTagPolicy = 'none' | 'add-only' | 'add-remove'

export interface ChatTag {
  id: string; name: string; color: string; order: number; status?: boolean
  agent?: AgentTagPolicy
  agent_provenanced?: boolean
  agent_store_degraded?: boolean
}

export type TagColumnMode = 'any' | 'all' | 'none'

/** Derived board lane a session can be in. Mirrors `_VALID_STATE_KEYS` in
 *  `dashboard/chat_tags.py`, which refuses a column naming anything else. The
 *  rules that map a slot onto one of these live in `pages/chat/sessionLane.ts`. */
export type SessionLaneKey = 'needs_approval' | 'waiting' | 'working' | 'idle'

/** A board column filters either by tags or by the session's live runtime lane.
 *  `source` is absent on every column persisted before lanes existed, so a
 *  missing value MUST be read as `'tags'`. A `'state'` column carries a
 *  `state_key` and ignores `tag_ids`/`mode`/`include_untagged` entirely. */
export interface TagColumn {
  id: string; name: string; tag_ids: string[]; mode: TagColumnMode; order: number; include_untagged?: boolean
  source?: 'tags' | 'state'
  state_key?: SessionLaneKey | ''
}

/** `GET /api/chat/slots/{slot}/side`: the side conversation the gateway holds in
 *  memory, which a reloaded panel restores from. Texts arrive redacted, as the
 *  side frames carry them; `id` is the one the frame that announced the row
 *  carried as `message_id`. */
export interface SideSnapshot {
  open: boolean
  created_at?: string
  last_run_id?: string
  /** A side turn is in flight, so a submit would steer or queue. */
  running?: boolean
  messages?: Array<{ id: string; run_id?: string; role: 'user' | 'assistant'; content: string; ts: string; steer?: boolean; is_error?: boolean }>
  /** `rev` counts the entry's edits, as the edit frame reports it, so a reader
   *  can tell which of two copies of the entry is newer. */
  queue?: Array<{ id: string; content: string; ts: string; rev?: number }>
}

export interface ChatMessage {
  role: string; content: string; cls: string; ts?: string
  /** Original unprocessed text — source of truth for reparse on stream completion. */
  rawText?: string
  /** Structured metadata for role-specific data (e.g. tool_input for permission messages). */
  meta?: Record<string, unknown>
  /** Regenerated variants of an assistant message (most recent last). */
  variants?: { content: string; ts?: string; blocked_links?: unknown; redactions?: unknown }[]
  /** Which variant index is currently active. */
  variant_idx?: number
  /** Counter for consecutive identical tool message deduplication. */
  _toolCount?: number
  /** Message kind discriminator for special message types (e.g. 'stop_event'). */
  kind?: string
  /** On a `streaming` row of a slot snapshot: the newest chunk seq the server
   *  folded into it. The client seeds its replay guard (`lastChunkSeq`) from it
   *  so a live chunk that races the snapshot is not appended a second time.
   *  Absent from an older gateway, which means "apply every chunk as before". */
  seq?: number
  /** Gateway process generation that numbered `seq` (folded snapshot rows). */
  gen?: string
  /** One skill-selection decision the gateway stamped on the row that ends the
   *  turn, when the Decisions (Jev) seam answered for it. Typed `unknown`
   *  because the shape is validated at the read, by
   *  `pages/chat/decisionRecord.ts` — the strip draws nothing for a record it
   *  cannot check. The gateway stamps it under `meta` on both doors, and this
   *  top-level spelling is accepted as well, the split `kind` above has too. */
  decisions_strip?: unknown
  /** One risk annotation the gateway stamped on a TOOL row when the Decisions
   *  (Jev) seam answered `tool.risk` for that call. Typed `unknown` because the
   *  shape is validated at the read, by `pages/chat/toolRiskRecord.ts` — the
   *  badge draws nothing for a record it cannot check, and nothing for the `safe`
   *  tier. An ANNOTATION: the permission decision was taken without it, so a
   *  record never means the call was stopped or altered. Stamped under `meta` on
   *  both doors; this top-level spelling is accepted too, as `decisions_strip`
   *  and the split `kind` above are. */
  decisions_tool_risk?: unknown
}

/** GET /api/chat/slots/{slot}; identity is optional for older gateways. */
export interface ChatSlotDetailResponse extends TurnIdentityFields {
  key: string
  title?: string
  running: boolean
  stopping?: boolean
  messages: ChatMessage[]
  queue?: unknown[]
  total: number
  has_more: boolean
  next_before: number
  context_pct?: number
  context_used_tokens?: number
  context_window_tokens?: number
}

export interface SubagentActivity {
  id: string; task: string; agent: string
  /** The caller's short display name for the run (`spawn_run`'s `label`),
   *  folded from the spawn/done/snapshot frames. The task box shows it in place
   *  of the task text; absent when the caller gave none. */
  label?: string
  /** The wave this agent belongs to, absent for a solo spawn. Folded from the
   *  `batch_id` the gateway stamps on every spawn/done/snapshot frame; shown as
   *  a short batch chip in the Subagents panel so wave siblings group (#759). */
  batchId?: string
  /** Model the live session actually resolved to serve, '' when unknown. Folded
   *  from the `model` field on the `subagent_spawn`/`subagent_done`/snapshot WS
   *  frames; shown beside the agent pill in the Subagents panel so a model-pinned
   *  run's real model is visible (#3582). */
  model?: string
  /** The model pin the caller REQUESTED for this subagent (the `requested_model`
   *  field on spawn/snapshot frames). Present only when the caller supplied a pin;
   *  compared against `model` via `isModelDowngrade` to render the amber chip on
   *  the live card (#5326). */
  requestedModel?: string
  /** The sub-agent's OWN session key, where it writes its per-turn context
   *  rows. Carried on the `subagent_spawn`/`subagent_done`/snapshot frames
   *  (backend `conversation_key or subagent:<id>`). The Session Breakdown tree
   *  uses it to fetch this node's own context-trace so each node shows the
   *  composition of ITS window, not the parent's. Absent for native cards. */
  childSession?: string
  status: 'pending' | 'running' | 'tool' | 'done' | 'error' | 'stopped'
  streaming: string; lastTool: string
  startedAt: number; elapsed: number; credits?: number; error?: string
  /** True when `startedAt` was ASSUMED rather than observed, which is the case
   *  for an entry minted by `upsertSlotSub` from an incremental frame: that frame
   *  carries no start time, so the entry records its arrival instant. The agent
   *  may already have been running for minutes, so a row MUST NOT render an
   *  elapsed figure derived from it -- a card reading "3s" for a five-minute run
   *  is the two-numbers-disagree confusion the idle row exists to remove. Cleared
   *  by any frame that supplies real timing: a `subagent_snapshot` replay carries
   *  `started`, and `subagent_done` carries `elapsed`. */
  startedAtAssumed?: boolean
  toolCount?: number      // observed tool calls (incl. auto-approved) — running-card progress
  /** Recent tool calls in arrival order, newest last, capped at
   *  `SUBAGENT_TOOL_CALLS_CAP` (#13628). Live frames only: a card restored from
   *  disk after its run has no timeline. `toolCount` may exceed its length when
   *  the cap dropped old calls or the scale coalescer merged frames. */
  /** `id` is the call's `tool_call_id` when the frame carried one. */
  toolCalls?: { tool: string; id?: string }[]
  stalled?: boolean       // reaper flagged this subagent as idle/stalled
  /** Seconds of no stream activity measured when the reaper raised `stalled`
   *  (the `idle_secs` the backend already sends with `subagent_stalled`).
   *  Distinct from `elapsed` (total runtime): only the idle span justifies the
   *  warning, so the card shows this rather than making the user infer it. */
  idleSecs?: number
  /** Client clock when that stall frame arrived. The backend emits `idle_secs`
   *  ONCE, on the not-stalled→stalled transition, so this is what lets the row
   *  advance the figure instead of freezing it beside a live elapsed counter —
   *  while `stalled` holds there is by definition no activity to reset it. */
  stalledAt?: number
  retrying?: boolean      // transient-backend retry (or cancel auto-continue) in flight
  approval_id?: string
  approving?: boolean
  /** The `approval_id` a resolve attempt found gone (`isTerminalApprovalRefusal`):
   *  decided or expired where this client could not see it. Scoped by id so a
   *  fresh approval on the same card is decidable again; the card itself stays
   *  pending, since no outcome is known. */
  approvalGone?: string
  /** The authoritative spawn inventory found no process for a gone approval.
   *  Distinguishes a never-launched retired card from a user-stopped run. */
  approvalRetired?: boolean
  /** Inline terminal output for native (`native:*`) cards only. Native cards
   *  cannot lazy-load from disk (no SubagentManager record), so the bounded
   *  done-event result is stored here. Managed cards leave this unset and use
   *  the on-demand DiskLoader to keep Redux memory bounded. */
  result?: string
}

/** Where `clampToolOutput` (wire.ts in store/chat) removed the middle of a tool
 *  payload: the stored string is `head + '\n' + tail`, `at` is the offset of
 *  the tail (right after that newline) and `count` is how many characters were
 *  dropped between the two. Renderers put the localized marker there at view
 *  time, so the store never holds a rendered string and the marker follows a
 *  later language switch. */
export interface ToolPayloadCut {
  at: number
  count: number
}

export interface ToolActivity {
  type: string
  text: string          // tool name (or approval / activity label)
  purpose?: string      // tool purpose
  input?: string        // tool input (commands, file content, etc.)
  output?: string       // tool output (stdout, results, etc.)
  input_cut?: ToolPayloadCut   // set only when `input` was clamped
  output_cut?: ToolPayloadCut  // set only when `output` was clamped
  /** True while `output` holds only a MID-STREAM frame (a shell's streamed
   *  stdout): the call is still running. Cleared by its terminal result. */
  output_partial?: boolean
  ts: number
  execution_started_at?: number // when execution began (after approval); survives remount
  auto?: boolean        // auto-approved tool call
  approval_id?: string  // pending approval ID
  approval_type?: string // 'chat' or 'spawn'
  tool_call_id?: string  // for matching tool results
  rejected?: boolean     // true when approval was rejected
  kind?: string          // ACP tool kind; execute is the legacy shell signal
  is_shell?: boolean     // shell tools can expose an indeterminate live status
  tool_name?: string     // trusted programmatic tool name (_meta.kiro.toolName), when sent
  mcp_server?: string    // MCP server that served the call (_meta.kiro.mcpServerName), when sent
}

/** Parsed content block produced by the block assembler. */
export type BlockType = 'markdown' | 'code' | 'diff' | 'mermaid' | 'excalidraw' | 'widget'
export interface ContentBlock {
  type: BlockType
  content: string
  language?: string
  complete: boolean
  /** 1-based line in the original raw source where this block starts. */
  startLine?: number
  /** Set on a finalized fence whose closing delimiter never appeared. Its
   * content may include the rest of the reply, so it must stay visible. */
  unclosed?: boolean
  /** Artifact slug (widget blocks only) — when present, the widget is
   * already saved as an artifact in the user's library. The dashboard uses
   * this to render the bookmark filled, link the title to /artifacts/<slug>,
   * and treat clicks as un-save rather than save. */
  slug?: string
  /** This markdown block is one paragraph-bounded piece of the trailing
   * markdown block. `from` is that block's startLine and `part` this piece's
   * index, so a piece keeps its React key while lines above it are
   * reclassified. Set while streaming, and after the stream ends in the view
   * that streamed it; never on a message mounted already finished. */
  split?: { from: number; part: number }
}

export interface Notification {
  kind: string; title: string; body: string; ts: string
  acked?: boolean; job_id?: string; task_id?: string; approval_id?: string
  /** OS-banner collapse tag the gateway stamps on every delivered note
   *  (`native_notification_tag`). The service worker's Web Push banner carries
   *  the same value, so one event shows one banner. Absent on client-made notes. */
  tag?: string
  slot?: string; session_key?: string; slack_link?: string
  /** A Crew Mode crew member's approval: the key of the crew member that waits.
   *  `slot` then names its conductor, whose approval card is the only place it is
   *  decided, so the note offers no Approve or Reject of its own. Client-made. */
  crew_member?: string
  /** Transcript identity used to resume a closed slot; the slot name alone may name no history. */
  history_key?: string
  // RFC Phase 3: schema-v2 routing + per-channel settings stamps
  source?: string; channel?: string; priority?: string; silenced?: boolean
  // RFC Phase 4: inline actions, stacking, dashboard-internal deep link
  group_key?: string; url?: string
  actions?: { id: string; label: string; url?: string }[]
  /** The crewmate that published the note, stamped by the gateway from the
   *  publishing session. Persisted rows are untrusted: read it through
   *  `noteMember`, which checks the shape. */
  member?: { slug: string; name: string }
}

/** One row from GET /api/notifications/channels. */
export interface NotificationChannel {
  channel: string
  source: string
  registered: boolean
  default_priority: string | null
  protected: boolean
  settings: { muted?: boolean; priority?: string }
}

export interface PendingApproval {
  origin?: 'native' | 'coordinator'
  request_mid?: string
  /** A coordinator record's instance: pins a decision to that one record. */
  request_instance?: string
  tool_purpose?: string
  tool: string
  tool_input: string
  tool_kind: string
  request_id: string
  /** "1" when the slot approve route would honor a `trust` decision for it. */
  trust_grantable?: string
}

export interface SubagentInfo {
  id: string; task: string; done: boolean; error?: string; result?: string
  parent?: string
  /** The terminal-state contract for a finished run (`SubagentInfo.outcome`). */
  outcome?: 'completed' | 'failed' | 'stopped'
  agent?: string
  started?: number
  last_tool?: string
  stopped?: boolean
  awaiting_approval?: boolean
}

export interface SessionInfo {
  key: string; title?: string; messages: number; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary'
}

/** One row of a Crew Mode root's worker list (`GET /api/chat/slots/{root}/crew-workers`). */
export interface CrewWorker {
  key: string
  title: string
  agent: string
  /** `working` while a turn runs, `needs_approval` while one waits on a tool
   *  approval, `asking` while an old question card is open, `idle` otherwise,
   *  `closed` once the tab is gone. */
  status: 'working' | 'idle' | 'needs_approval' | 'asking' | 'closed'
  /** The side panel's group: a tool approval only the person can give, moving,
   *  waiting on its conductor, not started, finished. */
  group: 'approval' | 'working' | 'with_conductor' | 'waiting' | 'done'
  /** ISO time of the worker's last turn or message; "" when it has none yet. */
  last_activity: string
  /** The work-ledger item the worker is bound to, or "" when it is not bound. */
  ledger_item: string
  /** One line on the task: its latest report or title on the ledger, else its
   *  last reply; "" when it has neither yet. */
  summary?: string
  /** The session that dispatched it: the root itself or another worker. */
  created_by: string
  /** Its direct dispatcher by the lineage the server walks (the parent edge,
   *  `created_by`, the saved creator): the root, a sub-conductor's key, or "". */
  dispatched_by?: string
}

/** One bar on a Crew Mode timeline lane, from recorded transcript rows only. */
export interface CrewTimelineSegment {
  kind: 'turn' | 'tool' | 'waiting'
  start: string
  end: string
  /** `recorded`: a row stamps the end. `next_row`: the end is not stamped, so the
   *  bar stops at the next row the session wrote. `now`: still open. */
  end_bound: 'recorded' | 'next_row' | 'now'
  label: string
}

/** A point on a lane: an error row (`failed`) or a refused tool (`refused`). */
export interface CrewTimelineMark {
  kind: 'failed' | 'refused'
  ts: string
  label: string
}

/** One session's lane: the conductor first, then its workers. */
export interface CrewTimelineLane {
  key: string
  title: string
  agent: string
  model: string
  /** `served`: the backend reported it. `chosen`: the session's own pick.
   *  `default`: the session runs the gateway default (`model` names it, `auto`
   *  when the backend chooses). */
  model_source: 'served' | 'chosen' | 'default'
  role: 'conductor' | 'worker'
  status: CrewWorker['status']
  /** The Workers list's group for this session, by the same server rule. */
  group: CrewWorker['group']
  running: boolean
  waiting: boolean
  segments: CrewTimelineSegment[]
  marks: CrewTimelineMark[]
  credits: number
  first_ts: string
  last_ts: string
  /** Older bars left out to bound the reply; 0 when every bar is here. */
  dropped_segments: number
  /** A closed worker's lane only: false when its saved transcript cannot be read,
   *  so it has no bars to draw. */
  record?: boolean
}

/** A dispatch: the sender's `session_send` call to the worker's seed row. */
export interface CrewTimelineHandoff {
  from: string
  to: string
  start: string
  end: string
}

/** `GET /api/chat/slots/{root}/crew-timeline`. */
export interface CrewTimeline {
  root: string
  now: string
  live: boolean
  started: string
  ended: string
  elapsed_ms: number
  credits: number
  lanes: CrewTimelineLane[]
  handoffs: CrewTimelineHandoff[]
  /** What the transcript does not record, so the view does not draw it. */
  gaps: string[]
}
