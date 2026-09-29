/** The chat state's shape: `ChatState`, the entity types it is built from, the
 *  initial value, and the one persisted key family it seeds from
 *  (`mc-activity-open:<slot>`). */
import { safeSetItem } from '../../utils/safeStorage'
import type { PhaseDetail, ToolPhaseDetail } from '../../utils/toolStatusLabel'
import type { ChatMessage, SessionInfo, SubagentActivity, ToolActivity } from '../../types'
import type { SubagentQueuedReason } from '../../pages/chat/subagentQueuedReason'
import type { McpAppRenderPayload } from '../../lib/mcpAppSrcdoc'
import type { AutomationRecord } from '../../monitoring/automation'
import type { ErrorReport } from '../../utils/errorReport'
import type { HistoryDeleteRefusal } from '../../utils/historyDeleteRefusal'
import type { ParkedThinking } from './thinking'

/** Per-slot activity-panel open/closed state, persisted to localStorage so the
 *  panel's open/closed choice survives a full page reload — keeping it
 *  consistent with the tab strip, which already persists per-slot
 *  (mc-panel-tabs:<slot>).
 *  Mirrors the dashboardSlice pattern: seed initialState.slotActivity from this
 *  map, write on every activityOpen change. */
const ACTIVITY_OPEN_PREFIX = 'mc-activity-open:'          // one key per slot
/** Read every persisted per-slot activityOpen flag (mc-activity-open:<slot>). */
const loadActivityOpenMap = (): Record<string, boolean> => {
  const out: Record<string, boolean> = {}
  if (typeof localStorage === 'undefined') return out
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k || !k.startsWith(ACTIVITY_OPEN_PREFIX)) continue
      const slot = k.slice(ACTIVITY_OPEN_PREFIX.length)
      if (slot) out[slot] = localStorage.getItem(k) === 'true'
    }
  } catch { /* enumerating storage can throw in locked-down envs */ }
  return out
}
export const persistActivityOpen = (slot: string | null, open: boolean): void => {
  if (!slot) return
  safeSetItem(ACTIVITY_OPEN_PREFIX + slot, String(open))
}
/** Seed the per-slot activity buckets from the persisted open map so the first
 *  switchSlot on cold load restores each chat's panel open/closed state (the
 *  bucket's toolLog/subagents are runtime-only and start empty). */
const seedSlotActivity = (): ChatState['slotActivity'] =>
  Object.fromEntries(
    Object.entries(loadActivityOpenMap()).map(([k, open]) => [k, { toolLog: [], subagents: {}, activityOpen: open }]),
  )

export type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

/** Live progress entry for a dynamic-workflow run. Folded from workflow_run_event
 *  WS messages so the chat can show status while a run executes. */
export interface WorkflowRunProgress {
  run_id: string
  name: string
  phase: string
  lastLog: string
  status: 'running' | 'finished' | 'failed' | 'cancelled'
  error?: string
  sessionKey?: string
}

export interface SideMessage {
  role: 'user' | 'assistant'
  content: string
  ts: string
  run_id?: string
  is_error?: boolean
  /** Injected into a turn that was already running, not asked from idle. */
  steer?: boolean
  /** Shown before the server confirmed it, so it can be found again by identity.
   *  Position is not usable: an in-flight turn's frames interleave, and both the
   *  reconcile and the rollback used to guess this row was simply the last one. */
  optimistic?: boolean
}

/** One side question held behind an in-flight side turn. */
export interface SideQueueEntry {
  id: string
  content: string
  ts: string
  /** Set when this card is a steer the backend could not confirm and requeued.
   *  The card's id is brand new, so this is the only handle the submitting client
   *  has to recognise its own question — the broadcast content is redacted. */
  steerId?: string
  /** This client typed the content, so it is unredacted. A scrubbed broadcast edit cannot
   *  overwrite it — see the edit branch of `sseSideQueue`. */
  raw?: boolean
}

export interface SideState {
  messages: SideMessage[]
  lastRunId?: string
  pending?: boolean
  streaming?: boolean
  /** Questions queued behind the running turn, oldest first. */
  queue?: SideQueueEntry[]
  /** Text a cancel released, waiting for the panel to put it in the composer.
   *  Set by whichever convergence path lands first; cleared once consumed, so a
   *  lost HTTP response cannot mean lost text and neither path double-applies. */
  releasedText?: string
  /** Queue ids that have reached a TERMINAL state (drained or cancelled).
   *  A submit's HTTP callback can run after the frame that removed its entry,
   *  and re-pushing then shows a card the server no longer has — one that 404s
   *  on cancel. The server cannot rule this out for us: its `still_queued`
   *  answer is already stale by the time the callback runs. Bounded, because
   *  only the recent past can still be raced. */
  removedQueueIds?: string[]
  openedAtTurnCount: number
  createdAt: string
}

/**
 * One agent-authored follow-up suggestion.
 *
 * `prompt` is the expanded, self-contained handoff instruction — it is what
 * gets pre-filled into a composer; `title`/`description` are display only.
 * `branch` is an optional git branch name for the worktree route; when absent
 * the card derives one from the title. Server-side, every string here has
 * already been length-capped, sanitized, and credential/URL-redacted
 * (`SUGGEST_FOLLOWUP_SCHEMA` + `_redact_followup_item`), and `branch` is
 * regex-gated — but it is still LLM-authored text, so render it as text and
 * never as markup.
 */
export interface FollowupItem {
  title: string
  description: string
  prompt: string
  branch?: string
}

/** A slot's live status line, keyed by `kind`: a `tool` phase carries the
 *  agent-written `purpose` (plus the `toolCallId` it describes, so a refinement
 *  of the SAME call merges into it); a fixed phase carries no copy, and a
 *  server-supplied status carries its `label`. Purpose and label are separate
 *  fields so a reader cannot paint one for the other; `toolStatusLabel`
 *  resolves any of them into the string a row shows. */
export type SlotStatusDetail = ((ToolPhaseDetail & { toolCallId?: string }) | PhaseDetail) & { ts: number }

export interface ChatState {
  activeSlot: string | null
  messages: ChatMessage[]
  slotRunning: boolean
  slotStopping: boolean
  slotState: SlotState
  slotStatusDetail: Record<string, SlotStatusDetail>
  slotHasMore: boolean
  slotOldestIndex: number
  /** Slot the cursor above describes. A switch moves activeSlot first, so
   *  without this the cursor silently reads as the new chat's. */
  slotCursorKey: string | null
  /** requestId of the switchSlot fetch in flight, else null. While set, that
   *  switch owns the cursor: a background settle must not re-key it, and
   *  clearing the transcript must not install a cursor over it. */
  slotSwitchRequestId: string | null
  /** Slot the in-flight switch targets; it only installs a cursor for that one. */
  slotSwitchTarget: string | null
  /** Pre-switch selection, recorded by `switchSlot.pending` so `rejected` can
   *  restore it when the target turns out to be GONE (404). `pending` mutates
   *  four things atomically -- `activeSlot`, the outgoing slot's activity, its
   *  message page, and the MRU -- and the large majority of dispatch sites
   *  never `.unwrap()`, so the unwind must live here, where the pre-switch
   *  state is still in hand (#6309; caller-side compensation re-derived three
   *  distinct bugs on #6260). When the outgoing view is itself PROVISIONAL
   *  (its own switch never settled), `pending` keeps the previous settled
   *  origin instead of recording the half-loaded key, so a rapid A→B→C chain
   *  whose C fails falls back to A, not to a B that never finished loading.
   *  `cursor` is the outgoing slot's paging cursor when it described that slot
   *  at capture time, else null (no valid cursor existed, so a restore
   *  honestly leaves paging un-keyed rather than guessing); `olderError` rides
   *  along so the origin's top-of-transcript retry bar survives the round trip.
   *  MAINTENANCE: this snapshot is a manual enumeration of the live-pane
   *  fields. A new per-pane field must join BOTH halves of the pair -- capture
   *  here (or seed its per-slot map, as `slotRun` does) in `pending`, restore
   *  in `rejected` -- or it silently leaks across a failed switch. */
  slotSwitchOrigin: {
    key: string
    cursor: { hasMore: boolean; nextBefore: number; olderError: boolean } | null
    /** The active run mirror at capture time, restored verbatim. Kept CURRENT
     *  by the non-active run writers themselves (`syncOriginRun` at every
     *  `slotRun` state write), so a transition mid-flight lands in the
     *  snapshot as an event rather than being inferred afterwards -- inference
     *  by comparing `slotRun` cannot distinguish a same-value round trip (a
     *  queued turn completing writes idle over idle) from "never moved", and
     *  restoring the stale snapshot on that path resurrects a finished turn's
     *  busy composer. `running` is carried separately from `state`: a
     *  running-but-not-yet-streaming turn legitimately reads state 'idle'
     *  while running is true, so deriving one from the other drops it. */
    run: { state: SlotState; running: boolean; stopping: boolean }
  } | null
  /** A user-facing switch gesture hit a session the server no longer has
   *  (#6372). ChatPage renders it through the pane-level ErrorNotice — the
   *  `errors-use-error-notice` surface — above the composer. Carries the
   *  NAME, not the sentence, so the copy re-resolves on locale switch; ''
   *  when the slot list no longer knew the title. The optional report keeps
   *  the API endpoint, status, and backend code available to Ask the agent
   *  while the displayed sentence stays localized. Cleared by the next
   *  `switchSlot.pending` or the notice's dismiss. */
  switchSlotGone: { name: string; kind: 'gone' | 'failed'; report?: ErrorReport } | null
  loadingOlder: boolean
  /** Last older-history fetch was rejected; surfaced on the top-of-transcript bar. */
  slotOlderError: boolean
  lastChunkSeq: number | undefined
  /** The gateway process generation `lastChunkSeq` was numbered by (see
   *  chunk_generation server-side); a chunk or snapshot from a different
   *  generation replaces the floor instead of being ordered against it. */
  lastChunkGen: string | undefined
  _wsChunkedDuringFetch: boolean
  /** How many `chat_message` frames were dropped as redeliveries (see
   *  `isRedeliveredMessage`), across every slot, for the life of this tab.
   *
   *  Diagnostic, not product state: nothing renders it. It exists because the
   *  dedup makes at-least-once delivery INVISIBLE — the duplicate bubbles were
   *  the only user-facing signal that something upstream re-emits frames after a
   *  restart, and that source is still unidentified. A non-zero count here is
   *  that signal, and it survives in a Redux state dump rather than in console
   *  scrollback. Steady state on a healthy gateway is 0. */
  _redeliveredFramesDropped: number
  history: SessionInfo[]
  historyHasMore: boolean
  historyOffset: number
  /** The last resume that did not land the user in a session they can use, or
   *  null. This is the ONE post-resolve check for every resume entry point
   *  (#5925): the predicates live in `resumeFromHistory`'s own cases, so a
   *  caller does not have to re-derive "did this resume actually work" to give
   *  feedback -- and the two command-palette providers, which are plain modules
   *  with no component of their own, get feedback they could not render
   *  themselves.
   *
   *  `reason` separates the two ways a resume disappoints, because they need
   *  different sentences: `surface` means it succeeded but landed on a surface
   *  the chat page cannot display, `failed` means it did not succeed at all
   *  (a rejected request, or a fulfilled payload that says `ok: false`). Before
   *  the `failed` half existed, the rarer path was the very dead click this
   *  field was added to kill.
   *
   *  Raw facts, not a sentence: the render site localizes the surface label
   *  from `key`, which only it knows how to read. Lifecycle matches the
   *  per-component notice #3640 shipped -- cleared on dismiss or on the next
   *  resume attempt. */
  unresumableResume: { key: string; title: string; surface: string; reason: 'surface' | 'failed' } | null
  /** requestId of the most recent `resumeFromHistory.pending`. Latest-click-
   *  wins for the notice above: rapid clicks each start a resume, and an
   *  EARLIER one resolving after a LATER one must not narrate a row the user
   *  has already moved past. Supersedes the sidebar's component-local
   *  sequence ref, which could only order ITS OWN clicks -- a palette resume
   *  racing a sidebar resume was unordered before. */
  lastResumeRequestId: string | null
  /** A history delete the gateway REFUSED (409 with a `code`), sibling of
   *  `unresumableResume` above and rendered at the same site. The row is still
   *  in `history` -- nothing was deleted -- so without this the click looked
   *  dead: `api.deleteSession` throws on any non-2xx and nothing narrated it.
   *  Raw facts, not a sentence: the render site localizes from `code` (see
   *  utils/historyDeleteRefusal) while `report` keeps the API journal context
   *  for ErrorNotice's agent hand-off. Cleared on dismiss or on the next attempt. */
  undeletableHistory: HistoryDeleteRefusal | null
  pendingInput: string | null
  /** Transient feedback for agent-rebind failures shared by the picker and
   *  global cycle shortcuts. The App shell owns rendering and expiry. */
  agentSwitchNotice: { message: string } | null
  // True while a createSlot POST is in flight. Lets every New Chat entry
  // point show a pending state so the UI never looks dead on click.
  creatingSlot: boolean
  /** requestId of the most recent FOREGROUND create (one that will take focus)
   *  still in flight; null once it resolves. ChatPage snapshots the composer
   *  each time this changes, so a snapshot always belongs to one create. A
   *  background create never sets it. */
  foregroundCreateId: string | null
  /** The slot a foreground create ACTIVATED and that create's requestId,
   *  cleared when the next foreground create starts. ChatPage carries text
   *  typed during a create only on this activation, and only when the
   *  requestId matches its snapshot (see ChatPage's create-carry note). */
  lastCreatedActivation: { slot: string; requestId: string } | null
  slotContextPct: Record<string, number>
  // Real token counts behind the context ring (from the adapter usage_update),
  // keyed by slot. Used for the ring tooltip so "44%" shows its absolute
  // "used / window" tokens and can't be misread (e.g. 44% of 200k, not 1M).
  /** Per-slot absolute context token counts from the adapter's usage_update, so
   *  the ring tooltip can show "used / window" rather than a bare percentage.
   *  `used` is OPTIONAL: a reading seeded from a cold session's stored snapshot
   *  knows the window but not a measured used-count, and both consumers render
   *  an absent `used` as an approximation (a `~` prefix, derived from pct)
   *  rather than asserting a precise figure. */
  slotContextTokens: Record<string, { used?: number; window: number }>
  voicePlaying: boolean
  voiceAudio: string | null  // base64 stitched MP3 for replay
  subagents: Record<string, SubagentActivity>
  /** Aggregate "waiting to start" count per slot — agents accepted but queued
   *  behind the concurrency cap / stagger gate (no individual card yet). Keyed
   *  by slot name so it survives active-slot switches without the subagents
   *  map's active/non-active split. Populated by `subagent_queued` WS events. */
  subagentQueued: Record<string, number>
  /** Why the slot's queued agents wait, from the same `subagent_queued` event:
   *  the gate's `reason` kind plus the memory figures for the memory kinds.
   *  Absent for a slot exactly when the gateway sent a bare count (an older
   *  gateway, or nothing labelled), and the chips then keep their default
   *  "queued behind the concurrency limit" text. Cleared with the count. */
  subagentQueuedReason: Record<string, SubagentQueuedReason>
  /** The authoritative automation record for each bare slot key.
   *
   * Structured monitors remain here after reaching a terminal outcome so the
   * dashboard can explain the stop and offer the explicit restart route.
   * Legacy goal loops keep their historical presence-means-active behavior.
   * Both REST snapshots and WS frames pass through the same pure normalizer
   * before reaching this collection, so the sidebar and detail surface cannot
   * disagree about transport fields or status. */
  automations: Record<string, AutomationRecord>
  /** Agent id the user picked from the chip — the Activity Subagents tab
   *  scrolls to, expands, and auto-loads this card (1-click transcript). */
  selectedSubagentId: string | null
  toolLog: ToolActivity[]
  /** Live dynamic-workflow runs keyed by run_id. Populated from
   *  `workflow_run_event` WS broadcasts; consumed by WorkflowProgressBar. */
  workflowRuns: Record<string, WorkflowRunProgress>
  activityOpen: boolean
  activityTab: 'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'
  /** Monotonic counter bumped ONLY by `openActivityToTab` — i.e. only when
   *  something deliberately asks for a view (a slash command, a sub-agent /
   *  workflow card, a keyboard shortcut). The side panel's tab strip owns which
   *  tab is focused and persists that per chat, so a consumer must distinguish a
   *  genuine request from `activityTab` merely taking a new VALUE: switching
   *  chats restores the incoming chat's cached tab (defaulting to Files), and
   *  treating that as a request would force-focus Files or the last requested
   *  view over the tab the user actually left the chat on. */
  activityTabRequest: number
  /** Pending "reveal in sidebar" request, or null. State, not a window event, on
   *  purpose: the sidebar is unmounted while the drawer is collapsed (and under
   *  preview expand mode / on mobile), and a one-shot CustomEvent dispatched before
   *  the listener mounts is silently dropped — there is no replay. Held here, the
   *  request survives until the sidebar consumes and clears it in an effect that
   *  also runs on mount (issue #912).
   *
   *  ONE field carrying its `kind`, not one field per kind. The two targets are
   *  addressed by different identities (a slot key vs a folder id), which argued for
   *  two fields — but both identities are a single string, so `target` needs no
   *  narrowing at its one read site, and the pair had a cost the single field does
   *  not: two pending requests could exist at once, and since the sidebar's two
   *  effects run in declaration order, an older request could execute last and
   *  cancel a newer one's retry loop. With one field there is only ever one pending
   *  reveal, so the ordering is a property of the state rather than something a
   *  cross-field nonce comparison has to restore. */
  revealRequest: { kind: 'session' | 'folder'; target: string; nonce: number } | null
  /** Never-reset counter feeding `revealRequest.nonce`, so revealing the same
   *  session (or folder) twice produces two distinct requests (a key-only request
   *  would make the second reveal indistinguishable from the first). Monotonic
   *  across clears. */
  revealNonce: number
  /** Tool call to highlight & auto-expand inline. Set by openActivityToTool;
   *  consumed (cleared) once the matching ToolCallLine has expanded itself. */
  focusToolCallId: string | null
  /** MCP Apps (SEP-1865) render payloads keyed by tool_call_id. Populated from
   *  `mcp_app_render` WS broadcasts; consumed by ToolCallLine → McpAppFrame.
   *  tool_call_ids are globally unique (ACP-issued), so a flat map is safe
   *  across slots. */
  mcpApps: Record<string, McpAppRenderPayload>
  slotActivity: Record<string, { toolLog: ToolActivity[]; subagents: Record<string, SubagentActivity>; activityTab?: 'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'; activityOpen?: boolean }>
  slotSide: Record<string, SideState>
  slotSideClosed: Record<string, boolean>
  slotMessages: Record<string, ChatMessage[]>
  /** Fresh `has_more` for a BACKGROUND pane, written by every bounded warm.
   *  The pane's own query is staleTime:Infinity, so its has_more freezes at
   *  mount while a later warm can truncate the cache past the bound. */
  slotPaneHasMore: Record<string, boolean>
  /** Row count of a bounded pane hydrate, so the unbounded refetch a starting
   *  turn issues can supersede it and still keep the rows it never fetched.
   *  Absent once superseded, so the upgrade happens at most once per slot. */
  slotPaneBounded: Record<string, number>
  /** The server's own message count for a slot, as of the last slot-detail fetch.
   *
   *  This exists to tell two indistinguishable populations apart at the warm
   *  merge. Both are rows this pane holds after the anchor that the fetched page
   *  omits, and neither position, `meta.mid`, `ts` nor the optimistic flag
   *  separates them:
   *
   *    - a row that arrived by live stream after the page was built -- the server
   *      HAS it, so its count did not fall, and the row must be kept;
   *    - a row another client rewound or regenerated away -- the server no longer
   *      has it, so its count FELL, and the row must not be put back on screen.
   *
   *  A fall in this count is therefore the truncation signal. It is retained per
   *  slot because a single response cannot show a delta, and it is written by
   *  every slot-detail reducer so the value a warm compares against is the last
   *  one actually observed rather than a stale figure from an earlier pane.
   *
   *  Residual, stated rather than implied: a rewind followed by enough new turns
   *  to restore the count before this pane is warmed again reads as unchanged, so
   *  that interleaving is not covered. Absent a retained count there is nothing
   *  to compare and the merge declines to discriminate, keeping the rescue. */
  slotServerTotal: Record<string, number>
  /** Dispatch order of the warm whose response set `slotServerTotal`. Present
   *  only when that count came from a warm carrying one, so an absent entry
   *  means the ordering is unknown and the merge must not act on it. */
  slotServerTotalSeq: Record<string, number>
  /** Reasoning blocks whose anchoring row is above the loaded window, per slot.
   *  Client-only, so this is their only copy until the anchor pages back in. */
  thinkingOrphans: Record<string, Array<ParkedThinking<ChatMessage>>>
  /** Path B: per-slot live stream state so a non-active pane shows its own
   *  streaming/tool/idle indicator (mirrors slotActivity for tool events).
   *  `tick` is the entry's receipt order: `state` is written only through
   *  `setRunState`, which bumps it, so a point-in-time snapshot that captured
   *  the tick at dispatch can tell, at fulfillment, whether an ordered writer ran
   *  in between -- the ordering token a plain `state` cannot carry, because an
   *  idle written by a `_done` frame is indistinguishable from an idle left by
   *  an earlier turn. Read by `warmSlotCache` (captured at dispatch) and its
   *  `fulfilled` reducer (compared before writing). Absent reads as 0. The
   *  tick counts OBSERVED transitions only -- live frames, settlements, and
   *  the hand-back of an active mirror that moved -- never a warm's own
   *  write: two warms for one slot resolve in any order, so a warm that
   *  consumed the tick would make a NEWER snapshot read as stale. Warm
   *  against warm is ordered by `runWarmSeq`, the `warmSeq` of the newest
   *  warm whose run-state verdict was applied; an older warm landing after
   *  it declines. */
  slotRun: Record<string, { state: SlotState; lastChunkSeq?: number; lastChunkGen?: string; tick?: number; runWarmSeq?: number }>
  /** Path B: per-slot one-time hydration guard so the server history is
   *  prepended exactly once even if a WS frame seeds slotMessages first. */
  slotHydrated: Record<string, boolean>
  slotLoading: boolean
  slotHistory: string[]
  /** Whether a non-empty slots frame has arrived. Distinguishes a reconnect's
   *  empty frame, which must not tear anything down, from a genuinely empty
   *  list, which must. */
  slotsSnapshotSeen: boolean
  stopPressedAt: Record<string, number | null>
  /** Per-slot count of turn STARTS this tab has seen: a non-steer user frame,
   *  an inject row (cron / continue / auto-nudge), a local send, the active
   *  slot's server snapshot flipping to running, and the FIRST busy frame
   *  (chunk / tool / compacting) after idle. That last one matters: a user
   *  row typed in another dashboard tab is not broadcast (state.py skips
   *  `role == "user"` unless a channel replays it), so a background pane can
   *  see a new turn only as its chunks — without counting them, a settlement
   *  about the previous turn would idle the new one unchallenged. A frame of
   *  a turn already counted does not bump (the slot is no longer idle).
   *  Captured before a `/stop` request and, by `ChatPane`, on every render in
   *  which the snapshot reports running, then checked by
   *  `settleStopNotRunning` and the background branch of
   *  `syncSlotRunningFromServer`, so an answer or snapshot that was true for
   *  THAT turn cannot idle a NEWER one (#9547, GPT rounds 2 and 7). */
  runEpoch: Record<string, number>
  /** `runEpoch` of the active slot at the moment it became active. Read by
   *  `enterActiveSlot` when the slot is left: a mirror whose state equals the
   *  keyed entry's AND whose epoch is unchanged observed nothing while active
   *  (a provisional switch in and out), so the hand-back must not consume the
   *  entry's receipt tick -- a warm dispatched before the switch is still the
   *  newest view. A same-value round trip (idle -> a turn ran -> idle) always
   *  counts a turn start, so the epoch tells it apart from "never moved". */
  activeRunEpochAtEntry: number
  /** Pending ask_question cards keyed by slot. Keyed (rather than a single
   *  card) so concurrent ask_question calls from two slots cannot evict each
   *  other — the losing agent would block until its timeout. */
  pendingQuestions: Record<string, { slot: string; ask_id?: string; questions: Array<{ question: string; header?: string; options: Array<{ label: string; description?: string }>; multiSelect?: boolean }>; serverCardId?: string; native?: boolean; draftActive?: boolean }>
  // Agent-authored follow-up suggestions (suggest_followup MCP tool), rendered
  // as a card above the composer. Keyed BY SLOT: a single global card let a
  // suggestion arriving in session B silently evict session A's unacted-on card,
  // contradicting the documented per-session behaviour.
  //
  // `ts` is the broadcast timestamp, used to avoid clearing a card that arrived
  // while a slower action (worktree create) was still in flight.
  //
  // Ephemeral: this lives only in frontend state, so a full page reload drops it.
  // Deliberately NOT cleared by clearSlotState — a suggestion is not tied to an
  // in-flight turn, so tabbing away and back should still show it. Rendering is
  // gated on the active slot's own key, so a retained card can never surface
  // under the wrong session.
  followups: Record<string, { items: FollowupItem[]; ts: number }>
  // Post-titling "file this in <folder>?" offer, keyed by slot for the same
  // reason `followups` is: a card must never be evicted by, or surface under,
  // another session.
  //
  // Every string here is the user's own stored folder data — the backend model
  // call returns an INDEX into a folder list, never text — so nothing rendered
  // from this is model-generated (see chat_folder_suggest.py).
  //
  // Ephemeral like `followups`: frontend-only, dropped by a reload. The backend
  // offers at most one card per slot for the lifetime of that slot, so a
  // dismissed or lost card is never re-offered.
  //
  // `turns` counts the user sends that have happened since the card arrived, so
  // an unanswered card ages out instead of sitting above the composer for the
  // rest of the session (see FOLDER_SUGGESTION_MAX_TURNS). Ignoring a suggestion
  // IS an answer — the user who keeps typing has declined by conduct — and the
  // backend needs no telling because it never re-offers this slot anyway.
  folderSuggestions: Record<string, { folderId: string; folderName: string; breadcrumb: string; ts: number; turns: number }>
  // Slot with a locally-started turn awaiting server confirmation. While set,
  // the slots-sync ignores a server running=false for it (the snapshot may
  // predate the send). Cleared on server confirmation or turn end.
  pendingTurnSlot: string | null
}

export const initialState: ChatState = {
  activeSlot: null,
  messages: [],
  slotRunning: false,
  slotStopping: false,
  slotState: 'idle',
  slotStatusDetail: {},
  slotHasMore: false,
  slotOldestIndex: 0,
  slotCursorKey: null,
  slotSwitchRequestId: null,
  slotSwitchTarget: null,
  slotSwitchOrigin: null,
  switchSlotGone: null,
  loadingOlder: false,
  slotOlderError: false,
  lastChunkSeq: undefined,
  lastChunkGen: undefined,
  _wsChunkedDuringFetch: false,
  _redeliveredFramesDropped: 0,
  history: [],
  historyHasMore: false,
  historyOffset: 0,
  unresumableResume: null,
  lastResumeRequestId: null,
  undeletableHistory: null,
  pendingInput: null,
  agentSwitchNotice: null,
  creatingSlot: false,
  foregroundCreateId: null,
  lastCreatedActivation: null,
  slotContextPct: {},
  slotContextTokens: {},
  voicePlaying: false,
  voiceAudio: null,
  subagents: {},
  subagentQueued: {},
  subagentQueuedReason: {},
  automations: {},
  selectedSubagentId: null,
  toolLog: [],
  workflowRuns: {},
  activityOpen: false,
  activityTab: 'changes' as const,
  activityTabRequest: 0,
  revealRequest: null,
  revealNonce: 0,
  focusToolCallId: null,
  mcpApps: {},
  slotActivity: seedSlotActivity(),
  slotMessages: {},
  slotPaneHasMore: {},
  slotPaneBounded: {},
  slotServerTotal: {},
  slotServerTotalSeq: {},
  thinkingOrphans: {},
  slotRun: {},
  slotHydrated: {},
  slotLoading: false,
  slotSide: {},
  slotSideClosed: {},
  slotHistory: [],
  slotsSnapshotSeen: false,
  pendingQuestions: {},
  followups: {},
  folderSuggestions: {},
  stopPressedAt: {},
  runEpoch: {},
  activeRunEpochAtEntry: 0,
  pendingTurnSlot: null,
}
