import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { addSlotOptimistic, updateSlot, removeSlotOptimistic, markSlotRead, fetchSlots, slotSurfaceKey, sseSlots } from './dashboardSlice'
import { resolveDefaultColor } from '../utils/sessionColors'
import { gcSessionStorage } from '../utils/storageGc'
import type { RootState } from './index'
import type { ChatMessage, ChatSlot, SessionInfo, SubagentActivity, ToolActivity } from '../types'
import { SOFT_STOP_DEBOUNCE_MS, SPAWN_LAUNCH_MARKER } from '../pages/chat/types'
import { mergePreservedPastes } from '../utils/pasteTokens'
import { safeSetItem } from '../utils/safeStorage'
import type { McpAppRenderPayload } from '../lib/mcpAppSrcdoc'
import { i18nT } from '../i18n/t'
import { secureRandomId } from '../utils/secureId'

const SKIP_ROLES = new Set(['chunk', 'done'])
const filterMessages = (msgs: ChatMessage[]) => msgs.filter(m => !SKIP_ROLES.has(m.role))

/** Durable client-side identity for a message born WITHOUT a `ts` that will be
 *  mutated across dispatches (streaming/thinking accumulation). ChatPage keys
 *  rows by `meta.clientTs ?? ts` and falls back to a WeakMap id minted per
 *  message OBJECT — but Immer replaces the object on every `content +=` commit,
 *  so without a stamped identity a ts-less accumulating message would mint a
 *  NEW id (→ new React key → full row remount) on every chunk flush. That
 *  remount would reset useSmoothStream's reveal cursor (text snapping in whole
 *  chunks) and restart every CSS/Framer animation in the row
 *  (widget-placeholder dots flashing in unison). Stamping the identity once at
 *  append survives Immer's structural sharing for the message's whole life,
 *  including the streaming→assistant finalization that later sets a server
 *  `ts`. (This is the "durable id stamped in the reducer at append" that
 *  ChatPage's stableMsgKey comment points at.)
 *
 *  Uses a cryptographically-strong UUID (via secureRandomId) so message identity
 *  is exact and collision-free — no timestamp heuristics, no sequence numbers.
 *  The field is `meta.clientTs` for backward compatibility with existing
 *  renderers and the mergePreservedClientTs rehydration path. */
const mintMsgId = (): string => `msg-${secureRandomId()}`

/** Stamp a stable `meta.clientTs` on a message that has no server `ts` and no
 *  pre-existing client id. This makes every ts-less message carry a durable
 *  identity from birth, surviving Immer structural sharing, refetch/rehydration,
 *  and list replacement — closing the identity gap for error/system/permission
 *  messages that were previously only stable via WeakMap (object identity). */
const ensureMsgId = (msg: ChatMessage): ChatMessage => {
  if (msg.ts || (msg.meta as Record<string, unknown> | undefined)?.clientTs) return msg
  msg.meta = { ...(msg.meta || {}), clientTs: mintMsgId() }
  return msg
}

/** True when a WS chat frame is a REDELIVERY of a row the transcript already
 *  holds, so applying it again would render the same message twice — or, in the
 *  `assistant` branch, overwrite a live stream with stale text.
 *
 *  Identity is the server-minted row id `meta.mid` (`_ChatSlot.append`), and
 *  nothing else. The backend stamps it once per row and every door the row can
 *  arrive through carries it: the slot-detail HTTP rebuild, the live
 *  `chat_message` broadcast, and the JSONL round trip (persisted with `meta`,
 *  restored with it), so the two copies of one row are recognisably one row.
 *
 *  What this replaces, and why: a (`ts`, role, content) tuple cannot express
 *  this. A coarse OS clock stamps two rows appended in the same tick identically
 *  (the collision `mergePreservedClientTs` pass 1 already guards against) and two
 *  byte-identical messages are legitimate — a Slack channel window can replay
 *  exactly that pair. So a tuple either misses a redelivery (a duplicate bubble)
 *  or matches two distinct rows (a message silently disappears), and no tuning
 *  removes the ambiguity. An explicit id does.
 *
 *  A frame with NO `mid` is never treated as a duplicate: rows a client mints
 *  locally (streaming, thinking, optimistic bubbles) have no server identity yet,
 *  and channel-replayed rows genuinely carry no `meta` at all (`ConversationLog`
 *  writes only role/content/ts/source_* for those). Declining to dedup renders a
 *  duplicate at worst; guessing would drop a real message.
 *
 *  Called from ONE chokepoint per path, placed so it dominates every branch that
 *  creates OR mutates a row — the `tool` insert, the `assistant` reconcile (which
 *  overwrites the trailing `streaming` row, so a late redelivery of an old frame
 *  would clobber a NEW segment's live content), the `user` echo reconcile, and
 *  the generic push. A guard sitting after any of those is a guard some frame
 *  slips past.
 *
 *  Scans from the tail — a redelivery is almost always the newest row — but
 *  scans the whole list, since a replayed frame can be older. */
function isRedeliveredMessage(
  msgs: Array<{ meta?: Record<string, unknown> }>,
  meta?: Record<string, unknown>,
): boolean {
  const mid = meta?.mid
  if (typeof mid !== 'string' || !mid) return false
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].meta?.mid === mid) return true
  }
  return false
}

/** Finalize the most recent live `streaming` message in place (streaming →
 *  assistant), or drop it entirely when its content is a trivial placeholder
 *  the model emits before tool calls ("...", "…", "---", ". . .", etc.).
 *  Only patterns EXCLUSIVELY composed of 2+ repeated punctuation/whitespace
 *  chars are dropped — never single characters, which could be the start of
 *  legitimate content (list markers, etc.).
 *
 *  Shared by the two segment-finalize paths (active `sseChatMessage` and
 *  background `applyMessageToArray`) AND the steer insertion paths: a mid-turn
 *  steer bubble must never be pushed BELOW a live streaming message, or the
 *  chunk reducer (which scans backwards for the last `streaming` role) keeps
 *  streaming the rest of the segment into the stranded bubble ABOVE the steer
 *  card — the "streaming marker stuck at the steer point" bug. Freezing first
 *  means pre-steer text stays above the bubble and the next chunk opens a
 *  fresh streaming message below it. */
const finalizeTrailingStreaming = (msgs: ChatMessage[]) => {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'streaming') {
      const raw = msgs[i].content
      const isPlaceholder = !raw || (/^[\s.\-…·•–—]{2,}$/.test(raw) && /[.\-…·•–—]/.test(raw)) || raw === '…'
      if (isPlaceholder) {
        msgs.splice(i, 1)
      } else {
        msgs[i].role = 'assistant'
        msgs[i].rawText = msgs[i].content
      }
      break
    }
  }
}

/** The three keys that can pollute `Object.prototype` when used to index a
 *  plain-object map (`obj[key] = ...`). Slot ids, subagent ids, run ids, and
 *  session keys all flow in from WebSocket action payloads; a crafted payload
 *  carrying `__proto__` / `constructor` / `prototype` would otherwise mutate the
 *  shared prototype through the per-slot state maps in this slice. */
/** True if `key` would pollute the prototype chain if used to index a plain
 *  object. Every reducer that indexes a `Record<string, …>` state map by an
 *  externally-supplied key rejects such a key up front (early return) — an
 *  explicit guard the CodeQL prototype-pollution query recognizes as a barrier.
 *  It is the single fail-closed chokepoint; a dropped frame for a hostile key is
 *  the correct outcome (no legitimate slot/subagent/run id is `__proto__`).
 *  Written as explicit `===` comparisons (not a Set lookup) so static analysis
 *  can model it as a sanitizing guard. */
const isUnsafeKey = (key: string): boolean =>
  key === '__proto__' || key === 'constructor' || key === 'prototype'

/** Defense-in-depth companion to the early-return guards: reroutes a poisoned
 *  key to an inert own-property so any write that slips past a guard still can't
 *  reach the prototype. Real keys pass through unchanged. */
const safeKey = (key: string): string => (isUnsafeKey(key) ? `unsafe-key:${key}` : key)

/** Composite key for `state.mcpApps`: `<session>\u001F<tool_call_id>`. The
 *  session scope prevents cross-slot render collisions and makes per-slot
 *  eviction a prefix scan (the payloads carry multi-MB app HTML, so they must
 *  not outlive their slot). \u001F (unit separator) cannot appear in either
 *  component. */
export const mcpAppKey = (sessionKey: string, toolCallId: string): string =>
  `${sessionKey}\u001F${toolCallId}`

/** Max MCP App render payloads retained per slot (each carries multi-MB HTML);
 *  oldest are evicted past this bound. */
const MCP_APPS_PER_SLOT_MAX = 24

/** Drop every MCP App render payload belonging to `sessionKey` (slot deleted
 *  or its conversation cleared — the tool rows the apps hang off are gone). */
const evictMcpApps = (state: { mcpApps: Record<string, McpAppRenderPayload> }, sessionKey: string): void => {
  const prefix = `${sessionKey}\u001F`
  for (const k of Object.keys(state.mcpApps)) {
    if (k.startsWith(prefix)) delete state.mcpApps[k]
  }
}

/** Read one slot's pending question card, or null.
 *
 *  A bare `map[slot]` lookup is not safe even with guarded writes: for
 *  `__proto__` or `constructor` it returns an INHERITED value that is truthy but
 *  carries no `questions`, so the card renders and crashes. Guarding the key and
 *  requiring an own property makes the read fail closed. Exported so the single-
 *  chat view and the grid panes share one definition. */
export const pendingQuestionFor = (
  map: ChatState['pendingQuestions'] | undefined,
  slot: string | null | undefined,
): ChatState['pendingQuestions'][string] | null => {
  if (!slot || !map || isUnsafeKey(slot)) return null
  return Object.prototype.hasOwnProperty.call(map, slot) ? map[slot] : null
}

/** One queued-message entry as normalized by `fetchSlotDetail` from the backend
 *  slot-detail `queue` field. */
type SlotQueueItem = { content: string; queueId: string; ts: string }

/** SINGLE hydration path for the slot-detail `queue` field — the one place that
 *  turns backend queue entries into `queued` message bubbles. Every reducer that
 *  consumes a `fetchSlotDetail` payload (`switchSlot`, `warmSlotCache`,
 *  `refreshSlot`) routes through here so the hydration cannot be hand-copied and
 *  drift apart. Hand-copying it risks dropping queued messages: if `switchSlot`
 *  and `warmSlotCache` each mirror the same literal, a field added to one is
 *  silently forgotten in the other. Centralizing it means a new slot-detail
 *  payload field is added once and consumed everywhere.
 *
 *  Existing `queued` bubbles are stripped first so re-hydration is idempotent —
 *  a `queue_push` WS event may have appended a bubble during the HTTP fetch, and
 *  the server `queue` field is the canonical set. Returns a NEW array; queued
 *  bubbles are always appended last (after history), matching prior behavior. */
function hydrateQueuedBubbles(
  list: ChatMessage[],
  queue: SlotQueueItem[] | undefined,
): ChatMessage[] {
  const base = list.filter((m) => m.role !== 'queued')
  for (const { content, queueId, ts } of queue ?? []) {
    base.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId } })
  }
  return base
}

/** Single-sourced "N chunk(s) missed" degradation marker. Shared by the reducer's
 *  defensive non-batched path and the useWebSocket flush buffer (the live path)
 *  so the marker text and gap arithmetic cannot drift between the two copies.
 *  Returns '' when the seqs are adjacent (no gap). */
export const missedChunkMarker = (prevSeq: number, curSeq: number): string => {
  const missed = curSeq - prevSeq - 1
  return missed > 0 ? `\n[${missed} chunk(s) missed]\n` : ''
}

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
const persistActivityOpen = (slot: string | null, open: boolean): void => {
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

type SlotState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

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
}

export interface SideState {
  messages: SideMessage[]
  lastRunId?: string
  pending?: boolean
  streaming?: boolean
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

interface ChatState {
  activeSlot: string | null
  messages: ChatMessage[]
  slotRunning: boolean
  slotStopping: boolean
  slotState: SlotState
  slotStatusDetail: Record<string, { kind: string; text: string; ts: number; toolName?: string }>
  slotHasMore: boolean
  slotOldestIndex: number
  loadingOlder: boolean
  lastChunkSeq: number | undefined
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
  pendingInput: string | null
  // True while a createSlot POST is in flight. Lets every New Chat entry
  // point show a pending state so the UI never looks dead on click.
  creatingSlot: boolean
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
  /** Live goal-loop (auto-nudge) progress per slot, keyed by the BARE slot key
   *  the sidebar renders — `binding_key_for` strips the `dashboard:` prefix, so
   *  these match `Slot.key` directly. Channel loops (`slack:`/`discord:` keys)
   *  land here too and simply match no sidebar row.
   *  Only ACTIVE loops are held: a loop that hit `max_cycles` stays in the
   *  service registry with `active=false`, and a stopped loop must not keep
   *  showing progress, so presence in this map IS "looping".
   *  Cold-seeded from `GET /api/autonudge`, then kept live by `autonudge_state`
   *  WS events — the service emits one per fired cycle (autonudge.py
   *  `_emit("fired", …)` right after the `cycle_count` bump), which is what
   *  makes the counter tick without rebroadcasting the whole slots list. */
  goalLoops: Record<string, { cycle_count: number; max_cycles: number }>
  /** Agent id the user picked from the chip — the Activity Subagents tab
   *  scrolls to, expands, and auto-loads this card (1-click transcript). */
  selectedSubagentId: string | null
  toolLog: ToolActivity[]
  /** Live dynamic-workflow runs keyed by run_id. Populated from
   *  `workflow_run_event` WS broadcasts; consumed by WorkflowProgressBar. */
  workflowRuns: Record<string, WorkflowRunProgress>
  activityOpen: boolean
  activityTab: 'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'
  /** Monotonic counter bumped ONLY by `openActivityToTab` — i.e. only when
   *  something deliberately asks for a view (a slash command, a sub-agent /
   *  workflow card, a keyboard shortcut). The side panel's tab strip owns which
   *  tab is focused and persists that per chat, so a consumer must distinguish a
   *  genuine request from `activityTab` merely taking a new VALUE: switching
   *  chats restores the incoming chat's cached tab (defaulting to Files), and
   *  treating that as a request would force-focus Files or the last requested
   *  view over the tab the user actually left the chat on. */
  activityTabRequest: number
  /** Tool call to highlight & auto-expand inline. Set by openActivityToTool;
   *  consumed (cleared) once the matching ToolCallLine has expanded itself. */
  focusToolCallId: string | null
  /** MCP Apps (SEP-1865) render payloads keyed by tool_call_id. Populated from
   *  `mcp_app_render` WS broadcasts; consumed by ToolCallLine → McpAppFrame.
   *  tool_call_ids are globally unique (ACP-issued), so a flat map is safe
   *  across slots. */
  mcpApps: Record<string, McpAppRenderPayload>
  slotActivity: Record<string, { toolLog: ToolActivity[]; subagents: Record<string, SubagentActivity>; activityTab?: 'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'; activityOpen?: boolean }>
  slotSide: Record<string, SideState>
  slotSideClosed: Record<string, boolean>
  slotMessages: Record<string, ChatMessage[]>
  /** Path B: per-slot live stream state so a non-active pane shows its own
   *  streaming/tool/idle indicator (mirrors slotActivity for tool events). */
  slotRun: Record<string, { state: SlotState; lastChunkSeq?: number }>
  /** Path B: per-slot one-time hydration guard so the server history is
   *  prepended exactly once even if a WS frame seeds slotMessages first. */
  slotHydrated: Record<string, boolean>
  slotLoading: boolean
  slotHistory: string[]
  stopPressedAt: Record<string, number | null>
  /** Pending ask_question cards keyed by slot. Keyed (rather than a single
   *  card) so concurrent ask_question calls from two slots cannot evict each
   *  other — the losing agent would block until its timeout. */
  pendingQuestions: Record<string, { slot: string; ask_id?: string; questions: Array<{ question: string; header?: string; options: Array<{ label: string; description?: string }>; multiSelect?: boolean }> }>
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
  folderSuggestions: Record<string, { folderId: string; folderName: string; breadcrumb: string; ts: number }>
  // Slot with a locally-started turn awaiting server confirmation. While set,
  // the slots-sync ignores a server running=false for it (the snapshot may
  // predate the send). Cleared on server confirmation or turn end.
  pendingTurnSlot: string | null
}

const initialState: ChatState = {
  activeSlot: null,
  messages: [],
  slotRunning: false,
  slotStopping: false,
  slotState: 'idle',
  slotStatusDetail: {},
  slotHasMore: false,
  slotOldestIndex: 0,
  loadingOlder: false,
  lastChunkSeq: undefined,
  _wsChunkedDuringFetch: false,
  _redeliveredFramesDropped: 0,
  history: [],
  historyHasMore: false,
  historyOffset: 0,
  pendingInput: null,
  creatingSlot: false,
  slotContextPct: {},
  slotContextTokens: {},
  voicePlaying: false,
  voiceAudio: null,
  subagents: {},
  subagentQueued: {},
  goalLoops: {},
  selectedSubagentId: null,
  toolLog: [],
  workflowRuns: {},
  activityOpen: false,
  activityTab: 'files' as const,
  activityTabRequest: 0,
  focusToolCallId: null,
  mcpApps: {},
  slotActivity: seedSlotActivity(),
  slotMessages: {},
  slotRun: {},
  slotHydrated: {},
  slotLoading: false,
  slotSide: {},
  slotSideClosed: {},
  slotHistory: [],
  pendingQuestions: {},
  followups: {},
  folderSuggestions: {},
  stopPressedAt: {},
  pendingTurnSlot: null,
}

function pushHistory(history: string[], key: string): string[] {
  const deduped = history.filter(k => k !== key)
  deduped.push(key)
  return deduped.length > 50 ? deduped.slice(-50) : deduped
}

/**
 * Path B (native session grid): apply a WS chat frame for a NON-active slot
 * into the per-slot store so a pane rendering that slot streams live. The
 * ACTIVE-slot path in sseChatMessage is intentionally left byte-identical
 * (zero blast radius on the main chat); this mirrors the slotActivity tool
 * pattern already used for tool/subagent events on non-active slots.
 */
function applyNonActiveFrame(
  state: ChatState,
  p: { slot: string; role: string; content: string; ts?: string; seq?: number; cls?: string; meta?: Record<string, unknown>; kind?: string; batched?: boolean },
) {
  const { slot, role, content, ts, seq, cls, meta, kind, batched } = p
  if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
  const msgs = (state.slotMessages[safeKey(slot)] ??= [])
  const run = (state.slotRun[safeKey(slot)] ??= { state: 'idle' })
  const sa = (state.slotActivity[safeKey(slot)] ??= { toolLog: [], subagents: {} })
  const toolLog = sa.toolLog

  const effectiveKind = kind ?? (meta?.kind as string | undefined)
  if (effectiveKind === 'stop_event') {
    const id = (meta?.id as string) ?? ''
    const idx = id ? msgs.findIndex(m => m.meta?.id === id) : -1
    const msg: ChatMessage = ensureMsgId({ role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' })
    if (idx >= 0) msgs[idx] = msg
    else msgs.push(msg)
    return
  }
  if (role === '_segment') {
    finalizeTrailingStreaming(msgs)
    return
  }
  if (role === 'chunk') {
    run.state = 'streaming'
    // Drop only the EMPTY thinking placeholder (mirror the active
    // sseChatMessage path at chatSlice ~998), keeping content-bearing reasoning
    // blocks so a background pane's hydrated reasoning isn't silently deleted by
    // the next streamed chunk.
    if (msgs.some(m => m.role === 'thinking' && !m.content)) {
      const filtered = msgs.filter(m => !(m.role === 'thinking' && !m.content))
      msgs.length = 0
      msgs.push(...filtered)
    }
    const last = toolLog[toolLog.length - 1]
    if (last?.type === 'reasoning') last.text += content
    else {
      toolLog.push({ type: 'reasoning', text: content, ts: Date.now() })
      // Cap the non-active slot's tool log (mirrors the sseToolActivity cap)
      // so a long background-pane turn can't grow slotActivity without bound.
      if (toolLog.length > 100) toolLog.splice(0, toolLog.length - 100)
    }
    let streamIdx = -1
    for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'streaming') { streamIdx = i; break } }
    if (streamIdx >= 0) {
      const msg = msgs[streamIdx]
      // Share missedChunkMarker with the active path so the two cannot drift.
      // Skip on batched frames: the live WS flush buffer already owns gap
      // detection across the chunks it merges and inlines the marker into the
      // batch content, and it dispatches each batch carrying only the batch's
      // LAST seq. Comparing consecutive batches' last-seqs here would treat the
      // batch size as a gap and fabricate a false "[N chunk(s) missed]" marker
      // on every multi-chunk background-pane batch. Mirror the active path,
      // which guards the identical branch with `!batched`.
      if (!batched && seq !== undefined && run.lastChunkSeq !== undefined) {
        msg.content += missedChunkMarker(run.lastChunkSeq, seq)
      }
      msg.content += content
      msg.rawText = msg.content
    } else {
      msgs.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: mintMsgId() } })
    }
    if (seq !== undefined) run.lastChunkSeq = seq
    return
  }
  if (role === '_done') {
    run.state = 'idle'
    run.lastChunkSeq = undefined
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') { msgs[i].role = 'assistant'; msgs[i].rawText = msgs[i].content; break }
    }
    return
  }
  if (role === 'compacting') { run.state = 'compacting'; return }
  // Permission rows carry request_id/tool_input inside `cls` (JSON); lift it
  // here — BEFORE the guard — so the identity comparison sees the same
  // `tool_call_id` the stored row has.
  let effectiveMeta = meta
  if (role === 'permission' && !meta?.approval_id && cls) {
    try {
      const parsed = JSON.parse(cls)
      if (parsed.request_id) {
        effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
      }
    } catch { /* not JSON cls, ignore */ }
  }
  // Idempotent append — ONE chokepoint that dominates every branch below, which
  // is the point: each of those branches creates or mutates a row and returns,
  // so a guard placed after any of them is a guard some frame slips past.
  if (isRedeliveredMessage(msgs, effectiveMeta)) { state._redeliveredFramesDropped += 1; return }
  if (role === 'tool') {
    run.state = 'tool_running'
    let insertIdx = msgs.length
    if (insertIdx > 0 && msgs[insertIdx - 1]?.role === 'streaming') insertIdx--
    msgs.splice(insertIdx, 0, ensureMsgId({ role, content, cls: cls || '', ts, meta }))
    return
  }
  if (role === 'thinking') {
    if (!msgs.some(m => m.role === 'thinking')) msgs.push({ role: 'thinking', content: '', cls: '', meta: { clientTs: mintMsgId() } })
    return
  }
  if (role === 'assistant') {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') {
        msgs[i].role = 'assistant'; msgs[i].content = content; if (ts) msgs[i].ts = ts
        // Carry the frame's meta — crucially `mid`, this row's server identity.
        // The row was minted client-side by the first `chunk` and has none until
        // now; without it a later redelivery of THIS frame is unrecognisable and
        // would overwrite whatever is streaming at that moment.
        if (meta) msgs[i].meta = { ...(msgs[i].meta || {}), ...meta }
        return
      }
    }
  }
  if (role === 'user') {
    // A steered message does not start a new turn — skip the "stale permissions"
    // cleanup so the approval bar remains visible and answerable (#1667).
    if (!meta?.steer) {
      sa.toolLog = []
      for (const m of msgs) {
        if (m.role === 'permission' && !m.meta?.resolved) { if (m.meta) m.meta.resolved = 'rejected'; else m.meta = { resolved: 'rejected' } }
      }
    }
    // Reconcile the optimistic user bubble (appendSlotMessage) rather than
    // pushing a 2nd identical one when the server echoes the user frame — same
    // pattern as sseSideResult. Kills the during-turn duplicate user message.
    const lastUser = msgs[msgs.length - 1]
    if (lastUser?.role === 'user' && lastUser.content === content) {
      if (ts) lastUser.ts = ts
      if (meta) lastUser.meta = { ...(lastUser.meta || {}), ...meta }
      return
    }
  }
  msgs.push(ensureMsgId({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind }))
}

/** Path B selectors: read a slot's messages / stream-state, falling back to the
 *  global active mirror when the slot IS the currently-active one. */
const EMPTY_MESSAGES: ChatMessage[] = []
export const selectSlotMessages = (state: RootState, slot: string): ChatMessage[] =>
  slot === state.chat.activeSlot ? state.chat.messages : (state.chat.slotMessages[slot] ?? EMPTY_MESSAGES)
export const selectSlotStreamState = (state: RootState, slot: string): SlotState =>
  slot === state.chat.activeSlot ? state.chat.slotState : (state.chat.slotRun[slot]?.state ?? 'idle')

const EMPTY_TOOLLOG: ToolActivity[] = []
/** Per-slot tool log, falling back to the global active mirror. */
export const selectSlotToolLog = (state: RootState, slot: string | null): ToolActivity[] =>
  slot && slot !== state.chat.activeSlot ? (state.chat.slotActivity[slot]?.toolLog ?? EMPTY_TOOLLOG) : state.chat.toolLog
const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}
/** Per-slot subagent map, falling back to the global active mirror — the
 *  read-only selector twin of the internal `getSlotSubs`. Exists so the
 *  Activity panel can subscribe to this itself instead of having ChatPage hold
 *  the subscription and pass it down: ChatPage renders on every streamed token,
 *  and `sseSubagentChunk` bumps this reference per sub-agent chunk, so a
 *  ChatPage-level subscription re-rendered the whole page for a panel that is
 *  closed by default. */
export const selectSlotSubagents = (state: RootState, slot: string | null): Record<string, SubagentActivity> =>
  slot && slot !== state.chat.activeSlot ? (state.chat.slotActivity[slot]?.subagents ?? EMPTY_SUBAGENTS) : state.chat.subagents
/** Per-slot pending tool-approval (unresolved permission after the slot's last
 *  user message) — slot-aware version of ChatInput's old selectPendingApproval,
 *  so each grid pane's approval bar reflects ITS slot, not the global active one. */
export const selectSlotPendingApproval = (state: RootState, slot: string | null): ChatMessage | null => {
  const msgs = slot ? selectSlotMessages(state, slot) : state.chat.messages
  // Find the last NON-steer user message — steered messages don't start a new
  // turn, so they must not hide a pending approval bar (#1667).
  let lastUserIdx = -1
  for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'user' && !msgs[i].meta?.steer) { lastUserIdx = i; break } }
  for (let i = msgs.length - 1; i > lastUserIdx; i--) {
    const m = msgs[i]
    if (m.role === 'permission' && !m.meta?.resolved && m.meta?.approval_id) return m
  }
  return null
}

export const fetchHistory = createAsyncThunk(
  'chat/fetchHistory',
  async (append: boolean, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    const offset = append ? state.historyOffset : 0
    const d = await api.sessions(30, offset)
    return { sessions: (d.sessions || d) as SessionInfo[], hasMore: d.has_more || false, offset, append }
  },
)

async function fetchSlotDetail(key: string) {
  // No limit → backend returns all chained history (across gateway restarts).
  const d = await api.chatSlotDetail(key)
  type QueueItem = string | { content: string; id: string }
  return { key, messages: filterMessages(d.messages || []), running: d.running || false, stopping: d.stopping || false, hasMore: d.has_more || false, total: d.total || 0, queue: ((d.queue || []) as QueueItem[]).map((q: QueueItem) => typeof q === 'string' ? { content: q, queueId: crypto.randomUUID(), ts: new Date().toISOString() } : { content: q.content, queueId: q.id, ts: new Date().toISOString() }), context: d.context_pct != null ? { pct: d.context_pct, used: d.context_used_tokens ?? undefined, window: d.context_window_tokens ?? undefined } : undefined }
}

/** SINGLE hydration path for the slot-detail context-meter fields — the one
 *  place that seeds `slotContextPct`/`slotContextTokens` from HTTP. Every
 *  reducer consuming a `fetchSlotDetail` payload routes through here, for the
 *  same reason `hydrateQueuedBubbles` exists: three near-identical reducers
 *  hand-copying the same literal is how a field gets added to one and forgotten
 *  in the others.
 *
 *  Why it exists at all: `context_usage` WS frames are turn-scoped, so a
 *  session reopened in a fresh tab has no entry and the bar renders empty until
 *  the user sends a message.
 *
 *  A stale reading (recovered from the snapshot file because the session's ACP
 *  process is gone) arrives with `used` absent, because no process measured a
 *  count for it — the server omits it rather than relying on this client to
 *  drop it. The tooltip's existing `~` path is how that gets said out loud. The
 *  window is likewise often absent — kiro-cli reports a percentage far more
 *  often than absolute token counts — in which case no token entry is written
 *  at all and the meter keeps using its model-derived window.
 *
 *  Seeds ONLY when the slot has no entry yet. The backend broadcasts over WS
 *  before the HTTP response lands, so a turn's frame can arrive mid-fetch —
 *  an unconditional write would clobber measured live numbers with the older
 *  snapshot this request was built from. Absent-only is monotonic: it can fill
 *  a gap, never overwrite. */
function seedContextUsage(
  state: ChatState,
  key: string,
  context: { pct: number; used?: number; window?: number } | undefined,
): void {
  if (!context) return
  const k = safeKey(key)
  if (state.slotContextPct[k] !== undefined || state.slotContextTokens[k] !== undefined) return
  state.slotContextPct[k] = context.pct
  if (context.window) state.slotContextTokens[k] = { used: context.used, window: context.window }
}

export const switchSlot = createAsyncThunk(
  'chat/switchSlot',
  async (key: string, { dispatch }) => {
    dispatch(markSlotRead(key))
    return fetchSlotDetail(key)
  },
)

/** Re-fetch messages for a slot without changing activeSlot. Only applies if still active. */
/** Re-insert client-only reasoning (`thinking`) messages into a server-refreshed
 *  message list. The backend never persists reasoning, so a refresh (e.g. the
 *  one fired on chat_done) would otherwise drop the thinking block the instant a
 *  turn finishes. Each preserved block is anchored to the assistant message that
 *  immediately followed it in the old list (matched by finalized content) and
 *  re-inserted just before it. At most one reasoning block per assistant. Any
 *  block whose anchor isn't found is appended so it is never silently lost.
 *  Returns `incoming` unchanged (reference-equal) when there is nothing to
 *  preserve. */
function mergePreservedThinking<M extends { role: string; content: string; cls?: string }>(
  existing: M[],
  incoming: M[],
): M[] {
  const preserved: Array<{ msg: M; anchor: string | null }> = []
  for (let i = 0; i < existing.length; i++) {
    const m = existing[i]
    if (m.role !== 'thinking' || !m.content) continue
    let anchor: string | null = null
    for (let j = i + 1; j < existing.length; j++) {
      const r = existing[j].role
      if (r === 'assistant' || r === 'streaming') { anchor = existing[j].content.trimEnd(); break }
      if (r === 'user') break
    }
    preserved.push({ msg: m, anchor })
  }
  if (!preserved.length) return incoming
  const used = new Set<number>()
  const result: M[] = []
  for (const item of incoming) {
    if (item.role === 'assistant') {
      const c = item.content.trimEnd()
      for (let p = 0; p < preserved.length; p++) {
        if (!used.has(p) && preserved[p].anchor === c) {
          result.push({ ...preserved[p].msg }); used.add(p); break
        }
      }
    }
    result.push(item)
  }
  for (let p = 0; p < preserved.length; p++) {
    if (!used.has(p)) result.push({ ...preserved[p].msg })
  }
  return result
}

/** Carry the client-stamped `meta.clientTs` from the current messages onto the
 *  server copies returned by a slot-detail reload (the refreshSlot fired on
 *  chat_done). A message STREAMED this session is born with only
 *  `meta.clientTs` (a minted bornKey, no server `ts`); the reloaded server copy
 *  has an authoritative `ts` but NO `clientTs`. The renderer keys virtual rows
 *  by `clientTs ?? ts`, so without this the row's key flips bornKey → serverTs
 *  on the reload, remounting the row and DROPPING its measured height in the
 *  virtualizer's HeightCache — a visible scroll jump on every turn (the "reload
 *  the whole history, scroll bar keeps moving up, can't reach the bottom"
 *  report).
 *
 *  Matching is two-pass so a duplicate-content row can never steal a live
 *  identity (forward-first content matching would let an OLDER duplicate
 *  consume the newest stamp, flipping two rows' keys instead of zero):
 *    1. Durable identities — a stamp that already carries a server `ts` (it was
 *       reloaded before) matches its incoming copy by EXACT `ts`. Collision-proof.
 *    2. Freshly-streamed identities — a stamp with NO `ts` (born this session,
 *       not yet reloaded) has nothing to match on, but its server copy is the
 *       NEWEST message of that role, so pair newest-first: walk the ts-less
 *       stamps from the transcript tail and scan `incoming` in REVERSE for the
 *       first unused (normalized-role, trimmed-content) match. 'streaming' is
 *       normalized to 'assistant' since finalization flips the role.
 *  Returns `incoming` unchanged (reference-equal) when nothing needs carrying. */
function mergePreservedClientTs<M extends { role: string; content: string; ts?: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
): M[] {
  const norm = (r: string): string => (r === 'streaming' ? 'assistant' : r)
  const stamped = existing.filter(m => typeof m.meta?.clientTs === 'string')
  if (!stamped.length) return incoming
  const carried = new Array<string | undefined>(incoming.length)
  const usedIncoming = new Set<number>()
  let changed = false

  // Pass 1: durable (already-reloaded) stamps — same server `ts` AND matching
  // (normalized-role, trimmed-content). A `ts` is NOT unique (a coarse OS clock
  // can stamp two fast tool-delimited rows with the same tick) and is NOT
  // role-specific (a tool row can share the assistant's tick), so keying on ts
  // alone would (a) collapse two distinct same-ts identities or (b) hand a
  // stamp to the wrong row (e.g. an unstamped tool row ahead of the stamped
  // assistant). Bucket the stamps per ts and consume the first bucket entry
  // that also matches role+content, so each identity lands on its own row.
  const byTs = new Map<string, { ct: string; role: string; content: string }[]>()
  for (const s of stamped) {
    if (typeof s.ts === 'string' && s.ts) {
      const e = { ct: s.meta!.clientTs as string, role: norm(s.role), content: s.content.trimEnd() }
      const q = byTs.get(s.ts)
      if (q) q.push(e)
      else byTs.set(s.ts, [e])
    }
  }
  if (byTs.size) {
    for (let i = 0; i < incoming.length; i++) {
      const item = incoming[i]
      if (item.meta?.clientTs) continue
      if (!(typeof item.ts === 'string' && item.ts)) continue
      const q = byTs.get(item.ts)
      if (!q || !q.length) continue
      const irole = norm(item.role)
      const icontent = item.content.trimEnd()
      const qi = q.findIndex(e => e.role === irole && e.content === icontent)
      if (qi >= 0) { carried[i] = q[qi].ct; q.splice(qi, 1); usedIncoming.add(i); changed = true }
    }
  }

  // Pass 2: freshly-streamed (ts-less) stamps — pair newest-first from the tail.
  // Exclude still-'streaming' stamps (a partial in-progress row has no server
  // copy yet, so a content match could only hit an older duplicate) and
  // 'thinking' stamps (client-only, never present in the server payload — and
  // re-inserted separately by mergePreservedThinking), which also keeps this
  // pass from scanning one dead thinking stamp per turn.
  const tsLess = stamped.filter(
    s => !(typeof s.ts === 'string' && s.ts) && s.role !== 'streaming' && s.role !== 'thinking',
  )
  for (let p = tsLess.length - 1; p >= 0; p--) {
    const s = tsLess[p]
    for (let i = incoming.length - 1; i >= 0; i--) {
      if (usedIncoming.has(i)) continue
      const item = incoming[i]
      if (item.meta?.clientTs) continue
      if (norm(s.role) === norm(item.role) && s.content.trimEnd() === item.content.trimEnd()) {
        carried[i] = s.meta!.clientTs as string
        usedIncoming.add(i)
        changed = true
        break
      }
    }
  }

  if (!changed) return incoming
  return incoming.map((item, i) =>
    carried[i] !== undefined
      ? { ...item, meta: { ...(item.meta || {}), clientTs: carried[i] as string } }
      : item,
  )
}

export const refreshSlot = createAsyncThunk(
  'chat/refreshSlot',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot !== key) return null
    return fetchSlotDetail(key)
  },
)

/** Warm the per-slot message cache for a *background* slot once its turn
 *  finishes, so switching to it renders the completed answer instantly from
 *  cache instead of waiting for the on-switch fetch round-trip. Guarded to
 *  non-active slots; the fulfilled reducer writes only slotMessages[key] and
 *  never touches the active `messages`, so a background completion can't churn
 *  the view the user is currently looking at. Session-grid panes also rely on
 *  this to reconcile a background pane's optimistic/streamed/echoed messages to
 *  the server's canonical history at end-of-turn (replaces the earlier
 *  reconcileSlot thunk, which did the same job). */
export const warmSlotCache = createAsyncThunk(
  'chat/warmSlotCache',
  async (key: string, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (state.activeSlot === key) return null
    return fetchSlotDetail(key)
  },
)

export const createSlot = createAsyncThunk<
  ChatSlot,
  { agent?: string; model?: string; mode?: string; memory_mode?: string; clean_mode?: boolean; folder_id?: string | null; color_index?: number | null; project?: string | null; activate?: boolean } | string | undefined,
  { fulfilledMeta: { originActiveSlot: string | null; activate: boolean } }
>(
  'chat/createSlot',
  async (opts, { dispatch, getState, fulfillWithValue }) => {
    const agent = typeof opts === 'string' ? opts : opts?.agent
    const model = typeof opts === 'string' ? undefined : opts?.model
    const mode = typeof opts === 'string' ? undefined : opts?.mode
    const memory_mode = typeof opts === 'string' ? undefined : opts?.memory_mode
    const clean_mode = typeof opts === 'string' ? undefined : opts?.clean_mode
    const folderId = typeof opts === 'string' ? undefined : opts?.folder_id
    const explicitColor = typeof opts === 'string' ? undefined : opts?.color_index
    const project = typeof opts === 'string' ? undefined : opts?.project
    // `activate: false` creates the session WITHOUT stealing focus, so a caller
    // that must finish setting the slot up (e.g. scoping it to a worktree) can
    // do so before the user is able to type into it. Defaults to true — every
    // existing caller keeps the create-and-focus behaviour.
    const activate = typeof opts === 'string' ? true : opts?.activate !== false
    // Capture the active slot BEFORE the (potentially slow) create round-trip.
    // The fulfilled reducer compares this against the active slot at resolution
    // time: if the user switched to a different session while the create was
    // pending (e.g. New Chat spun on "Creating" under memory pressure and they
    // moved to another tab), the new slot must NOT hijack the view.
    const originActiveSlot = (getState() as RootState).chat.activeSlot
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, undefined, clean_mode, undefined, folderId || undefined)
    const dashState = (getState() as RootState).dashboard
    // An explicit color (e.g. carried from a slot being recreated on a
    // mode switch) wins; otherwise fall back to the default-color policy.
    const ci = explicitColor != null ? explicitColor : resolveDefaultColor(dashState.sessionDefaultColor, dashState.slots.length)
    if (ci != null) {
      slot.color_index = ci
      api.setSlotColor(slot.key, ci).catch(() => {})
    }
    // Folder membership rides the create payload above, so the server files the
    // slot before it broadcasts it. A follow-up PATCH would be too late to
    // matter: the slots frame announcing this slot is emitted before the create
    // response arrives here, so an unfiled slot would render at the top level
    // first and visibly jump into its folder.
    // Carry the project directory. The create endpoint ignores `project` and
    // defaults it to the workspace dir, so a recreated slot would otherwise
    // lose its project — re-apply it via the dedicated endpoint. (We do NOT
    // re-issue setSlotAgent here: that endpoint resets the project back to the
    // workspace default, which would clobber this carry. Agent rides the
    // create payload instead.)
    if (project) {
      slot.project = project
      if (activate) {
        api.chatSlotProject(slot.key, project).catch(() => {})
      } else {
        // Background create (activate: false): the caller is setting this slot up
        // and the user must not be able to reach it half-configured. Publishing it
        // via addSlotOptimistic makes it selectable from the sidebar immediately,
        // so a turn sent before scoping landed would run in the DEFAULT checkout.
        // Await the scope, and if it fails delete the session server-side rather
        // than publish an unscoped one.
        try {
          await api.chatSlotProject(slot.key, project)
        } catch (err) {
          await api.deleteChatSlot(slot.key).catch(() => {})
          throw err
        }
      }
    }
    dispatch(addSlotOptimistic(slot))
    // Carry the origin slot in the action meta (fulfillWithValue) rather than on
    // the payload, so it can never leak into the persisted slot object. The
    // fulfilled reducer reads action.meta.originActiveSlot to decide whether
    // activating the new slot is safe.
    return fulfillWithValue(slot, { originActiveSlot, activate })
  },
)

export const deleteSlot = createAsyncThunk(
  'chat/deleteSlot',
  async (key: string, { dispatch, getState }) => {
    const root = getState() as RootState
    const deletedSlot = root.dashboard.slots.find(s => s.key === key)
    // Use the surface key (forward-compat alias for `mode`) so a future
    // backend that emits a distinct `slot.surface` keeps "switch to a peer
    // session" pinned to the same nav destination.
    const deletedSurface = deletedSlot ? slotSurfaceKey(deletedSlot) : ''
    // Navigate before removeSlotOptimistic to prevent useEffect race
    if (root.chat.activeSlot === key) {
      const sameSurface = new Set(root.dashboard.slots.filter(s => slotSurfaceKey(s) === deletedSurface).map(s => s.key))
      const prev = root.chat.slotHistory.filter(k => k !== key && sameSurface.has(k)).pop()
        || root.dashboard.slots.filter(s => s.key !== key && sameSurface.has(s.key)).map(s => s.key)[0]
      dispatch({ type: 'chat/setActiveSlot', payload: null })
      if (prev) {
        await dispatch(switchSlot(prev)).unwrap().catch(() => dispatch({ type: 'chat/clearSlotState' }))
      } else {
        dispatch({ type: 'chat/clearSlotState' })
      }
    }
    dispatch(removeSlotOptimistic(key))
    try {
      await api.deleteChatSlot(key)
      gcSessionStorage(key)
    } catch {
      dispatch(fetchSlots())
      throw new Error('save failed')
    }
    return key
  },
)

export const resumeFromHistory = createAsyncThunk(
  'chat/resumeFromHistory',
  async ({ key, title }: { key: string; title: string }, { dispatch }) => {
    const d = await api.resumeChatSlot(key, title)
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: title || d.key, messages: 0, running: false, memory_mode: d.memory_mode, mode: d.mode, surface: d.surface ?? d.mode, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }))
      dispatch(updateSlot({ key: d.key, mode: d.mode, surface: d.surface ?? d.mode }))
    }
    return { ok: d.ok, key: d.key, messages: filterMessages(d.messages || []), hasMore: d.has_more || false, total: d.total || 0 }
  },
)

export const forkSlot = createAsyncThunk(
  'chat/forkSlot',
  async (
    { slot, atIndex, prompt, mode, direction }: { slot: string; atIndex?: number; prompt?: string; mode?: string; direction?: 'head' | 'tail' },
    { dispatch },
  ) => {
    const d = await api.forkChatSlot(slot, atIndex, prompt, mode, direction)
    if (d.ok) {
      dispatch(addSlotOptimistic({ key: d.key, title: d.title || d.key, messages: d.messages || 0, running: false, folder_id: d.folder_id }))
    }
    return d
  },
)

export const deleteHistorySession = createAsyncThunk(
  'chat/deleteHistorySession',
  async (key: string) => { await api.deleteSession(key); return key },
)

export const loadOlderMessages = createAsyncThunk(
  'chat/loadOlder',
  async (_, { getState }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!state.activeSlot || !state.slotHasMore || state.loadingOlder) return null
    if (state.slotOldestIndex <= 0) return null
    const d = await api.chatSlotDetail(state.activeSlot, 100, state.slotOldestIndex)
    return { messages: filterMessages(d.messages || []), hasMore: d.has_more || false, total: d.total || 0 }
  },
)

export const requestStop = createAsyncThunk(
  'chat/requestStop',
  async ({ slotId, force }: { slotId: string; force: boolean }, { getState, dispatch }) => {
    const state = (getState() as { chat: ChatState }).chat
    if (!force) {
      const lastPress = state.stopPressedAt[slotId] ?? 0
      if (Date.now() - lastPress < SOFT_STOP_DEBOUNCE_MS) return
    }
    dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: Date.now() }))
    try {
      if (force) {
        await api.stopChatSlotForce(slotId)
      } else {
        await api.stopChatSlot(slotId)
      }
    } catch {
      dispatch(chatSlice.actions.setStopPressedAt({ slotId, ts: 0 }))
    }
  },
)

/** Get subagents map for a slot (read-only lookup) */
function getSlotSubs(state: ChatState, slot: string) {
  return slot !== state.activeSlot ? state.slotActivity[slot]?.subagents : state.subagents
}

/**
 * Attach a tool result's output to the tool MESSAGE meta for every message
 * carrying `tid`, in both the live list and the per-slot cache.
 *
 * All matching messages, not just the newest: an auto-approved tool produces
 * TWO tool messages sharing one tool_call_id (🔧 pre-approval + ✅
 * post-approval) and the server patches both, so stopping at the first would
 * leave the pair disagreeing about the same call.
 */
function applyToolOutputToMessages(
  state: ChatState,
  slot: string,
  tid: string,
  output: string,
): void {
  if (isUnsafeKey(slot)) return
  const patch = (msgs: ChatMessage[] | undefined): void => {
    if (!Array.isArray(msgs)) return
    for (const m of msgs) {
      if (m.role !== 'tool') continue
      const meta = m.meta as Record<string, unknown> | undefined
      if (!meta || meta.tool_call_id !== tid) continue
      m.meta = { ...meta, output }
    }
  }
  if (slot === state.activeSlot) patch(state.messages)
  // The cache can hold the SAME array reference as state.messages (switchSlot
  // caches by reference), so this may be a second pass over one list — the
  // patch is idempotent, and skipping it would strand a genuinely separate
  // cached copy with no output. `safeKey` mirrors hydrateSlotMessages: the
  // early return above already rejects unsafe keys, this is the codebase's
  // defense-in-depth companion.
  patch(state.slotMessages[safeKey(slot)])
}

/** Central, fail-closed accessor for a single subagent entry by wire-supplied
 *  id. Applies the `isUnsafeKey` prototype-pollution guard once, here, so no
 *  reducer that indexes the subagents map by an external id has to remember the
 *  incantation — forgetting is impossible at the call site. A hostile
 *  `__proto__`/`constructor`/`prototype` id resolves to `undefined` (frame
 *  dropped) rather than to `Object.prototype`. */
function getSlotSub(state: ChatState, slot: string, id: string): SubagentActivity | undefined {
  if (isUnsafeKey(id)) return undefined
  return getSlotSubs(state, slot)?.[id]
}

/**
 * Live "sub-agents running" signal for a slot, derived from the
 * subagent_spawn/tool/done WS events (the only real-time source — see the
 * ChatSidebar countActive note: dashboardSlice fields only refresh on a full
 * slots push). Counts pending/running/tool as active, mirroring ChatSidebar.
 */
export const selectSlotSubagentsActive = (state: RootState, slot: string): boolean => {
  const subs = getSlotSubs(state.chat, slot)
  if (!subs) return false
  for (const a of Object.values(subs)) {
    if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') return true
  }
  return false
}

// Stable empty result so the selector is referentially stable (with shallowEqual)
// when a slot has no pending spawn approvals — avoids needless re-renders.
const _EMPTY_PENDING_SPAWNS: SubagentActivity[] = []

/**
 * Pending sub-agent SPAWN approvals for a slot — sub-agents queued to run but
 * blocked on the user's approval (status 'pending' + an approval_id).
 *
 * The backend broadcasts a spawn approval as a WS `approval` event with
 * id `spawn:<agent_id>`; useWebSocket routes it into `sseSubagentPending`, so
 * it only ever renders as a pending card in the side panel's Subagents tab —
 * there is NO inline chat prompt and NO notification. This selector lets the
 * composer surface a top-level "awaiting approval" banner so the user knows an
 * action is required without hunting through the side panel. Use with
 * `shallowEqual`.
 */
export const selectSlotPendingSpawnApprovals = (state: RootState, slot: string | null): SubagentActivity[] => {
  if (!slot) return _EMPTY_PENDING_SPAWNS
  const subs = getSlotSubs(state.chat, slot)
  if (!subs) return _EMPTY_PENDING_SPAWNS
  const out = Object.values(subs).filter(a => a.status === 'pending' && !!a.approval_id)
  return out.length ? out : _EMPTY_PENDING_SPAWNS
}

/**
 * Total sub-agents in flight across EVERY slot — started (running/tool/pending)
 * plus accepted-but-queued. Drives the Sessions rail activity dot, which is the
 * only cross-page signal that a background chat has agents working: the chip
 * above the composer covers the viewed slot only, and the sidebar subtitle is
 * invisible from any other page.
 *
 * Memoized (`createSelector`) because the surface registry invokes activity
 * selectors on every dispatch.
 */
export const selectSubagentActivityCount = createSelector(
  [
    (state: RootState) => state.chat.activeSlot,
    (state: RootState) => state.chat.subagents,
    (state: RootState) => state.chat.slotActivity,
    (state: RootState) => state.chat.subagentQueued,
  ],
  (activeSlot, activeSubs, slotActivity, queued) => {
    const countActive = (m?: Record<string, SubagentActivity>) => {
      if (!m) return 0
      let n = 0
      for (const a of Object.values(m)) {
        if (a.status === 'running' || a.status === 'tool' || a.status === 'pending') n++
      }
      return n
    }
    let total = activeSlot ? countActive(activeSubs) : 0
    for (const [slot, act] of Object.entries(slotActivity ?? {})) {
      // On switchSlot the active slot's map is aliased into both
      // state.subagents and slotActivity[active].subagents (same reference),
      // so this guard is what prevents double-counting it.
      if (slot === activeSlot) continue
      total += countActive(act.subagents)
    }
    for (const q of Object.values(queued ?? {})) total += q > 0 ? q : 0
    return total
  },
)

/**
 * Single source of truth for "is this slot's composer busy" — the signal that
 * queues the next message (busy affordance) and skips the optimistic user
 * bubble (the backend returns a "queued" message instead, so an optimistic
 * bubble would render a duplicate). Busy = main turn running OR background
 * sub-agents running, with two redundant sub-agent signals OR'd
 * (conservative): the live WS-derived signal (real-time, self-heals on
 * sub-agent crash via the reaper's done event) and the slots-stream snapshot
 * field (covers the first frames after reload/reconnect before WS events
 * replay). Used by ChatPage (main route) and ChatPane (split view) — keep both
 * routes on this selector so the rule cannot drift.
 */
export const selectComposerBusy = (state: RootState, slot: string | null): boolean => {
  if (!slot) return state.chat.slotRunning
  if (selectSlotStreamState(state, slot) !== 'idle') return true
  if (slot === state.chat.activeSlot && state.chat.slotRunning) return true
  if (selectSlotSubagentsActive(state, slot)) return true
  const dashSlot = state.dashboard.slots.find((sl) => sl.key === slot)
  // A running autopilot plan keeps the composer "busy" so a mid-plan message
  // queues (chip card) instead of rendering an optimistic bubble that would
  // duplicate the backend's queued message. slot.running reads False between
  // stages, so orchestrating is the durable signal here.
  return !!(dashSlot?.subagents_running || dashSlot?.orchestrating)
}

/** Roles the continue scans walk past: they are not the conversation's floor.
 *  Mirrors `_is_interrupted` / `_has_conversation` in
 *  `src/kiro_crew/dashboard/chat_handlers.py`, which likewise only read
 *  `user` / `assistant` / `error` rows. Keep them in sync — these predicates
 *  decide whether to OFFER Continue and what to call it, those decide whether to
 *  authorize it and what to tell the model. */
const CONTINUE_SCAN_SKIP = new Set(['queued', 'tool_call', 'tool_result', 'inject', 'subagent', 'permission', 'nudge'])

/**
 * True when the active slot can be handed back to the agent — i.e. Continue is
 * worth offering on an empty composer.
 *
 * The rule is simply "the slot is idle and has a conversation under it". It is
 * NOT limited to turns that visibly died, because a transcript cannot reliably
 * show that they did: a force-quit or force-exit runs no cleanup, so no error
 * row is ever written and a killed turn reads exactly like a finished one (see
 * ``_has_conversation`` in `src/kiro_crew/dashboard/chat_handlers.py`, which
 * authorizes the press under the slot lock). Offering it on every idle slot
 * covers those invisible interruptions, and doubles as a plain "keep going"
 * nudge — the one thing an empty composer's dead send button could never do.
 *
 * Everything that makes a continuation UNSAFE still returns false: a live turn,
 * a stop in flight, an optimistic local turn, a mid-plan autopilot slot, a
 * running subagent, or a queued message the runner is about to pick up itself.
 *
 * Computed locally on purpose: `messages`, `slotRunning`, `slotStopping` and the
 * queue are all already in this store, so no server field is needed to decide
 * what to SHOW. The server re-checks under the slot lock when the button is
 * actually pressed — this view is a lagging WS snapshot, so it cannot be the
 * authority for dispatching a turn.
 *
 * An empty transcript returns false, which keeps a brand-new chat's send button
 * disabled exactly as it is today.
 */
export const selectContinuable = (state: RootState): boolean => {
  const c = state.chat
  if (c.slotRunning || c.slotStopping || c.pendingTurnSlot) return false
  // An autopilot plan reads `running` False BETWEEN stages while still mid-plan,
  // so `running` alone would offer Continue on a slot the server refuses with
  // `slot_orchestrating`. Mirrors the same guard in `api_chat_slot_continue`.
  const dashSlot = state.dashboard.slots.find((sl) => sl.key === c.activeSlot)
  if (dashSlot?.orchestrating || dashSlot?.subagents_running) return false
  const msgs = c.messages
  if (!msgs.length) return false
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    // A pending queued message means the backend is about to run the thread on
    // its own — offering Continue would double-fire the turn.
    if (m.role === 'queued') return false
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      // Compaction notices are assistant-role system messages, not the floor.
      if (m.role === 'assistant' && (m.meta as { kind?: string } | undefined)?.kind === 'compaction') continue
      return true
    }
  }
  return false
}

/**
 * True when *m* is the card recorded because the USER pressed Stop.
 *
 * Two forms exist and both are load-bearing: the websocket path sets `kind` AND
 * `meta.kind` (see the stop_event branch in the message reducer), while a
 * transcript rehydrated from disk carries only the JSON-encoded `cls` that
 * `parse_cls_meta()` unpacks into `meta`. `ChatPage` and `ChatMessageList`
 * already test both; this is the same predicate named once so a fourth caller
 * cannot check only half of it.
 */
export const isStopEvent = (m: ChatMessage): boolean =>
  m.kind === 'stop_event' || (m.meta as { kind?: string } | undefined)?.kind === 'stop_event'

/**
 * True when the transcript SHOWS the last turn ending without the assistant
 * handing the floor back — the user's row is last, or an `error` row trails the
 * assistant's.
 *
 * Gates the composer's Resume button (composed with `selectContinuable` in
 * ChatPage) and selects the continuation body handed to the model. Mirrors
 * `_is_interrupted` in `src/kiro_crew/dashboard/chat_handlers.py` — the two must
 * agree, or the button promises one thing and the agent is told another.
 *
 * A false result means "nothing in the transcript proves an interruption", never
 * "the turn definitely finished": the force-quit case leaves no evidence.
 */
export const selectTurnInterrupted = (state: RootState): boolean => {
  const msgs = state.chat.messages
  let sawTrailingError = false
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    // A deliberate Stop ENDS the turn; it does not interrupt it. This must be
    // tested before the user/assistant check, because pressing Stop before the
    // reply produced any text leaves `[user, stop_event]` — shape-identical to
    // "the gateway died before anything came back", which is what this scan
    // would otherwise read it as. Without this branch the same visible action
    // (pressing Stop) offered Resume or not depending purely on whether a
    // segment had flushed first, i.e. on invisible timing the user cannot
    // predict. The user chose to stop; the floor is theirs, so the composer
    // shows Send. Reached only for the NEWEST turn's terminator — an older stop
    // card deeper in history is never scanned, because a later user/assistant
    // row returns first.
    if (isStopEvent(m)) return false
    if (m.role === 'error') { sawTrailingError = true; continue }
    if (CONTINUE_SCAN_SKIP.has(m.role)) continue
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      if (m.role === 'assistant' && (m.meta as { kind?: string } | undefined)?.kind === 'compaction') continue
      return m.role === 'user' ? true : sawTrailingError
    }
  }
  return false
}

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    setActiveSlot(state, action: PayloadAction<string | null>) { state.activeSlot = action.payload; state.slotState = 'idle'; state.pendingTurnSlot = null },
    clearSlotState(state) { state.messages = []; state.toolLog = []; state.subagents = {}; state.activityTab = 'files'; state.slotRunning = false; state.slotStopping = false; state.slotState = 'idle'; state.slotHasMore = false; state.slotOldestIndex = 0; state.loadingOlder = false; state.lastChunkSeq = undefined; state._wsChunkedDuringFetch = false; state.slotStatusDetail = {}; state.voicePlaying = false; state.voiceAudio = null; if (state.activeSlot) delete state.pendingQuestions?.[state.activeSlot]; state.pendingTurnSlot = null },
    setPendingInput(state, action: PayloadAction<string | null>) { state.pendingInput = action.payload },
    setQuestionCard(state, action: PayloadAction<{ slot: string; ask_id?: string; questions: ChatState['pendingQuestions'][string]['questions'] }>) {
      // Defensive init: existing test fixtures build partial preloaded state
      // without this key.
      if (!state.pendingQuestions) state.pendingQuestions = {}
      // Same fail-closed chokepoint as the neighbouring slot-keyed reducers: the
      // slot arrives over the websocket, and `__proto__`/`constructor` would
      // otherwise make a READ return an inherited value that is truthy but has
      // no `questions`, crashing QuestionCard on render.
      if (isUnsafeKey(action.payload.slot)) return
      state.pendingQuestions[safeKey(action.payload.slot)] = action.payload
    },
    clearQuestionCard(state, action: PayloadAction<{ slot: string }>) {
      if (isUnsafeKey(action.payload.slot)) return
      delete state.pendingQuestions?.[safeKey(action.payload.slot)]
    },
    /** Clear the card only if it is the one the backend just resolved.
     *  Guards against a stale `question_card_resolved` (from a timed-out
     *  earlier ask) wiping a newer card the user is mid-way through. */
    resolveQuestionCard(state, action: PayloadAction<{ ask_id: string }>) {
      // Delete by ask_id match so a stale resolution for an already-replaced
      // question cannot clear a different slot's live card.
      for (const [slotKey, card] of Object.entries(state.pendingQuestions ?? {})) {
        if (card?.ask_id === action.payload.ask_id) delete state.pendingQuestions[slotKey]
      }
    },
    setFollowupCard(state, action: PayloadAction<{ slot: string; items: FollowupItem[]; ts?: number }>) {
      const { slot, items, ts } = action.payload
      if (!slot || !items?.length) return
      if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
      // Defensive: a partial preloaded slice (tests, older persisted state) can
      // arrive without this key.
      if (!state.followups) state.followups = {}
      state.followups[slot] = { items, ts: ts ?? Date.now() / 1000 }
    },
    // `ts` guards the async case: "Start in new worktree" clears the card only
    // after its request resolves, and a NEWER card may have arrived for the same
    // slot meanwhile. Passing the ts the action started with means the newer card
    // survives instead of being clobbered by the older action's completion.
    clearFollowupCard(state, action: PayloadAction<{ slot: string; ts?: number }>) {
      const { slot, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const card = state.followups?.[slot]
      if (!card) return
      if (ts != null && card.ts !== ts) return
      delete state.followups[slot]
    },
    // Skip ONE suggestion without discarding the others. The card disappears
    // only once its last item is gone, so skipping the first of three does not
    // silently throw away the other two.
    dismissFollowupItem(state, action: PayloadAction<{ slot: string; index: number; ts?: number }>) {
      const { slot, index, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const card = state.followups?.[slot]
      if (!card) return
      // Same staleness guard as `clearFollowupCard`: a replacement card can land
      // between render and click, and an unqualified dismiss would delete that
      // index from a card the user has not seen.
      if (ts != null && card.ts !== ts) return
      const items = card.items.filter((_, i) => i !== index)
      if (items.length) state.followups[slot] = { ...card, items }
      else delete state.followups[slot]
    },
    setFolderSuggestion(state, action: PayloadAction<{ slot: string; folderId: string; folderName: string; breadcrumb: string; ts?: number }>) {
      const { slot, folderId, folderName, breadcrumb, ts } = action.payload
      if (!slot || !folderId || !folderName) return
      if (isUnsafeKey(slot)) return  // never index a state map with __proto__/constructor/prototype
      // Defensive: a partial preloaded slice (tests, older persisted state) can
      // arrive without this key.
      if (!state.folderSuggestions) state.folderSuggestions = {}
      state.folderSuggestions[slot] = { folderId, folderName, breadcrumb, ts: ts ?? Date.now() / 1000 }
    },
    // Both answers land here — accepting the move and declining it clear the same
    // way, because the backend keeps no state to resolve and offers at most one
    // card per slot either way. `ts` guards the async case the way
    // `clearFollowupCard` does: the accept path clears after its move request is
    // dispatched, so a card that arrived meanwhile must survive.
    clearFolderSuggestion(state, action: PayloadAction<{ slot: string; ts?: number }>) {
      const { slot, ts } = action.payload
      if (isUnsafeKey(slot)) return
      const card = state.folderSuggestions?.[slot]
      if (!card) return
      if (ts != null && card.ts !== ts) return
      delete state.folderSuggestions[slot]
    },
    sseContextUsage(state, action: PayloadAction<{ slot: string; pct: number; used_tokens?: number; window_tokens?: number; reset?: boolean }>) {
      const { slot, pct, used_tokens, window_tokens, reset } = action.payload
      if (isUnsafeKey(slot)) return
      state.slotContextPct[safeKey(slot)] = pct
      if (window_tokens && window_tokens > 0) {
        state.slotContextTokens[safeKey(slot)] = { used: used_tokens ?? 0, window: window_tokens }
      } else if (reset) {
        // Model switch / compaction / session reset: the stored counts belong
        // to a window that no longer describes the session. Deleting re-enables
        // the model-derived fallback (provider.getContextWindow(slot.model)).
        // A frame WITHOUT `reset` never deletes — it only fills or replaces — so
        // the backend sets `reset` whenever it has no real counts to send,
        // clearing stale counts instead of leaving them beside a fresh pct.
        delete state.slotContextTokens[safeKey(slot)]
      }
    },
    appendMessage(state, action: PayloadAction<ChatMessage>) {
      // Finalize-on-steer: a mid-turn steer bubble (ChatPage steer(), meta.steer)
      // must freeze the live streaming message BEFORE it is pushed, or the
      // chunk reducer keeps appending the rest of the segment into the stranded
      // streaming message ABOVE the bubble (stuck streaming marker at the steer
      // point). The backend cuts the segment at the same boundary (see
      // _run_chat's steer segment cut), so the frozen order matches the
      // persisted transcript and the chat_done refresh doesn't reorder it.
      const m = action.payload
      if (m.role === 'user' && m.meta?.steer) finalizeTrailingStreaming(state.messages)
      state.messages.push(ensureMsgId(m))
    },
    /** Optimistically append a message to a specific slot's store — global
     *  `messages` when it's the active slot, else `slotMessages[slot]`. Lets a
     *  grid pane show a just-sent user message immediately in the right place. */
    appendSlotMessage(state, action: PayloadAction<{ slot: string; message: ChatMessage }>) {
      const { slot, message } = action.payload
      if (isUnsafeKey(slot)) return
      const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[safeKey(slot)] ??= [])
      // Reconcile a steer echo (server 'steer_push', meta.steer, no optimistic
      // flag) against the optimistic bubble that steer() added client-side
      // (meta.optimistic). Update it in place rather than pushing a duplicate
      // user message — mirrors the user-frame reconcile in applyMessageToArray.
      //
      // The optimistic bubble is NOT necessarily the last message: a steer is
      // by definition sent mid-turn, so streaming/thinking/tool messages keep
      // landing between the optimistic append and the WS echo. A tail-only
      // check loses that race and renders a duplicate "Steered into the
      // running turn" card. Scan backwards (bounded) over optimistic STEER
      // bubbles only (a plain optimistic user message with coincidentally
      // identical text must never be consumed): prefer exactly matching
      // content (handles rapid back-to-back steers in order), else fall back
      // to the most recent one (server-side redaction can alter the echoed
      // content, so an exact match isn't guaranteed).
      if (message.role === 'user' && message.meta?.steer && !message.meta?.optimistic) {
        const floor = Math.max(0, msgs.length - 50)
        let target: ChatMessage | undefined
        let fallback: ChatMessage | undefined
        for (let i = msgs.length - 1; i >= floor; i--) {
          const m = msgs[i]
          if (m.role !== 'user' || !m.meta?.optimistic || !m.meta?.steer) continue
          if (message.content && m.content === message.content) { target = m; break }
          if (!fallback) fallback = m
        }
        const bubble = target ?? fallback
        if (bubble) {
          if (message.content) bubble.content = message.content
          // Preserve the optimistic (client-generated) ts as meta.clientTs
          // BEFORE overwriting with the server ts. The chat renderer keys
          // rows by `meta.clientTs ?? ts`; without this stash the ts change
          // would change the React key, remounting the bubble and replaying
          // the one-shot steer entrance animation (visible flicker).
          if (message.ts && bubble.ts && message.ts !== bubble.ts) {
            bubble.meta = { ...(bubble.meta || {}), clientTs: bubble.ts }
          }
          if (message.ts) bubble.ts = message.ts
          bubble.meta = { ...(bubble.meta || {}), ...(message.meta || {}) }
          delete (bubble.meta as Record<string, unknown>).optimistic
          return
        }
        // No optimistic bubble to reconcile — this tab did not initiate the
        // steer (another tab / a scene-interaction steer). Finalize-on-steer
        // before pushing, same as appendMessage: inserting the bubble below a
        // live streaming message strands the streaming marker above it. Only
        // done on the insert path — after a reconcile a NEW post-steer
        // streaming message may already be live below the bubble, and freezing
        // it here would wrongly finalize the in-flight stream.
        finalizeTrailingStreaming(msgs)
      }
      // Optimistic steer bubble from a pane-scoped composer: same freeze as the
      // appendMessage (active-slot) path.
      if (message.role === 'user' && message.meta?.steer && message.meta?.optimistic) {
        finalizeTrailingStreaming(msgs)
      }
      msgs.push(ensureMsgId(message))
    },
    updateStreamingMessage(state, action: PayloadAction<string>) {
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.content = action.payload }
      else { state.messages.push({ role: 'streaming', content: action.payload, cls: 'msg msg-a', meta: { clientTs: mintMsgId() } }) }
    },
    finalizeAssistant(state, action: PayloadAction<string | { content: string; ts?: string }>) {
      const payload = typeof action.payload === 'string' ? { content: action.payload } : action.payload
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.role = 'assistant'; last.content = payload.content; if (payload.ts) last.ts = payload.ts }
      else { state.messages.push({ role: 'assistant', content: payload.content, cls: 'msg msg-a', ts: payload.ts }) }
    },
    removeThinking(state) { state.messages = state.messages.filter(m => m.role !== 'thinking') },
    removeByApprovalId(state, action: PayloadAction<string>) { state.messages = state.messages.filter(m => m.meta?.approval_id !== action.payload) },
    resolveByApprovalId(state, action: PayloadAction<{ id: string; decision?: string }>) {
      const decision = action.payload.decision || 'approved'
      let m = state.messages.find(m => m.meta?.approval_id === action.payload.id)
      if (!m) {
        for (const arr of Object.values(state.slotMessages)) {
          const f = arr.find(x => x.meta?.approval_id === action.payload.id)
          if (f) { m = f; break }
        }
      }
      if (m?.meta) m.meta.resolved = decision
      // If rejected, mark the matching toolLog entry so the pill can show a rejection icon
      const toolCallId = m?.meta?.tool_call_id as string | undefined
      if (decision === 'rejected' && toolCallId) {
        const log = state.toolLog
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && log[i].tool_call_id === toolCallId) {
            log[i].rejected = true; break
          }
        }
      }
    },
    /** Mark all unresolved permission messages as resolved (e.g. when stop is pressed). */
    clearPendingPermissions(state) {
      for (const m of state.messages) {
        if (m.role === 'permission' && !m.meta?.resolved) {
          if (m.meta) m.meta.resolved = 'rejected'
          else m.meta = { resolved: 'rejected' }
        }
      }
      // Mark all incomplete toolLog entries as rejected so pills show the right icon
      for (const e of state.toolLog) {
        if (e.type === 'tool' && e.output == null && !e.rejected) e.rejected = true
      }
    },
    setSlotRunning(state, action: PayloadAction<boolean>) {
      state.slotRunning = action.payload
      if (!action.payload) state.pendingTurnSlot = null
    },
    /** Optimistically start a turn for `slot` after a local send. Marks it
     *  pending so the slots-sync won't clobber running=true before the server
     *  catches up. Only the active slot drives the visible footer. */
    startLocalTurn(state, action: PayloadAction<string>) {
      const slot = action.payload
      state.pendingTurnSlot = slot
      if (slot === state.activeSlot) state.slotRunning = true
    },
    /** Reconcile the active slot's running state from a WS slots broadcast.
     *  running=true is always trusted (also catches Slack/cron-initiated turns);
     *  running=false is ignored while a local turn is pending confirmation, since
     *  the snapshot may predate the send. Turn end is owned by _done/refreshSlot. */
    syncSlotRunningFromServer(state, action: PayloadAction<{ slot: string; running: boolean; stopping: boolean }>) {
      const { slot, running, stopping } = action.payload
      if (slot !== state.activeSlot) return
      if (running) {
        state.slotRunning = true
        state.slotStopping = stopping
        state.pendingTurnSlot = null
      } else if (state.pendingTurnSlot !== slot) {
        state.slotRunning = false
        state.slotStopping = stopping
      }
      // Pending turn: ignore both fields so a leftover stopping=true from a
      // prior turn can't falsely show a "stopping" state on the new turn.
    },
    setSlotStopping(state, action: PayloadAction<boolean>) { state.slotStopping = action.payload },
    setStopPressedAt(state, action: PayloadAction<{ slotId: string; ts: number }>) {
      if (isUnsafeKey(action.payload.slotId)) return
      state.stopPressedAt[safeKey(action.payload.slotId)] = action.payload.ts
    },
    setSlotState(state, action: PayloadAction<SlotState>) { state.slotState = action.payload },
    setSlotStatusDetail(state, action: PayloadAction<{ slot: string; kind: string; text: string; ts: number; toolName?: string }>) {
      const { slot, ...detail } = action.payload
      if (isUnsafeKey(slot)) return
      state.slotStatusDetail[safeKey(slot)] = detail
    },
    clearMessages(state) { state.messages = []; state.slotHasMore = false; state.slotOldestIndex = 0; state.voiceAudio = null; state.voicePlaying = false; if (state.activeSlot) evictMcpApps(state, state.activeSlot) },
    truncateAfterIndex(state, action: PayloadAction<number>) { state.messages = state.messages.slice(0, action.payload) },
    replaceMessages(state, action: PayloadAction<ChatMessage[]>) { state.messages = action.payload },
    /** Path B: seed a non-active slot's message history into the per-slot store
     *  (one-time hydrate on pane mount). Prepends the server history BEFORE any
     *  frames that already arrived live: applyNonActiveFrame seeds slotMessages
     *  via `??= []` on the first WS frame, so `cur` can be non-empty before this
     *  hydrate fetch resolves. A dedicated `slotHydrated` flag makes it fire
     *  exactly once, so a racing frame can't make us silently drop history.
     *  No-op for the active slot (its mirror is already live). */
    hydrateSlotMessages(state, action: PayloadAction<{ slot: string; messages: ChatMessage[] }>) {
      const { slot, messages } = action.payload
      if (isUnsafeKey(slot)) return
      if (slot === state.activeSlot) return
      if (state.slotHydrated?.[slot]) return
      const cur = state.slotMessages[slot] ?? []
      state.slotMessages[safeKey(slot)] = [...messages, ...cur]
      if (!state.slotHydrated) state.slotHydrated = {}
      state.slotHydrated[safeKey(slot)] = true
    },
    setVoicePlaying(state, action: PayloadAction<boolean>) { state.voicePlaying = action.payload },
    setVoiceAudio(state, action: PayloadAction<string | null>) { state.voiceAudio = action.payload },
    toggleActivity(state) { state.activityOpen = !state.activityOpen; if (!state.activityOpen) state.focusToolCallId = null; persistActivityOpen(state.activeSlot, state.activityOpen) },
    openActivityPanel(state) { state.activityOpen = true; persistActivityOpen(state.activeSlot, true) },
    openActivityToTab(state, action: PayloadAction<'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'>) { state.activityOpen = true; state.activityTab = action.payload; state.activityTabRequest += 1; state.focusToolCallId = null; persistActivityOpen(state.activeSlot, true) },
    /** Tool details expand inline in the chat. This action signals the matching
     *  ToolCallLine pill to auto-expand and scroll into view. */
    openActivityToTool(state, action: PayloadAction<string>) { state.focusToolCallId = action.payload },
    /** Clear after the matching pill has consumed the focus signal, so the same trigger
     *  doesn't re-fire on subsequent re-renders. */
    clearFocusToolCallId(state) { state.focusToolCallId = null },
    /** Drop the previous connection's ephemeral subagent view before the gateway
     *  replays its authoritative running/done snapshot. Without this reset, an
     *  empty replay leaves agents from a restarted gateway visible indefinitely.
     *  Pending spawn-approval cards are preserved: the subscribe_subagents replay
     *  only re-emits native + managed running/done agents, so a card still
     *  awaiting approval has no backend SubagentInfo to hydrate it and would be
     *  lost (its approve/reject UI along with it) on a mid-approval reconnect. */
    clearSubagentsForSnapshot(state) {
      const keepPending = (subs: Record<string, SubagentActivity> | undefined): Record<string, SubagentActivity> => {
        const kept: Record<string, SubagentActivity> = {}
        if (subs) for (const [id, a] of Object.entries(subs)) if (a.status === 'pending') kept[id] = a
        return kept
      }
      state.subagents = keepPending(state.subagents)
      for (const activity of Object.values(state.slotActivity)) activity.subagents = keepPending(activity.subagents)
      // Queued counts are advisory and re-emitted on the next drain — reset to
      // avoid showing a stale "waiting" count for a wave that finished during
      // the disconnect (under-count self-heals on the next drain frame).
      state.subagentQueued = {}
    },
    /** Aggregate "waiting to start" count for a slot. Agents queued behind the
     *  concurrency cap / stagger gate have no individual card; this count lets
     *  the chip appear immediately on spawn and show how many are pending
     *  start (issues: late chip, flicker, invisible queue). */
    sseSubagentQueued(state, action: PayloadAction<{ slot: string; queued: number }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const n = Math.max(0, Math.floor(Number(action.payload.queued) || 0))
      // Tolerate a store built from partial preloaded state (test fixtures and
      // any consumer that predates this key): indexing an absent map throws and
      // would drop the queue update entirely.
      state.subagentQueued ??= {}
      if (n === 0) delete state.subagentQueued[safeKey(action.payload.slot)]
      else state.subagentQueued[safeKey(action.payload.slot)] = n
    },
    /** Replace the whole goal-loop map from a cold `GET /api/autonudge` seed.
     *  A full replace (not a merge) is correct here: the response is the
     *  service's complete registry, so a loop this client still holds but the
     *  server no longer reports has ended and must disappear. */
    setGoalLoops(state, action: PayloadAction<{ slot: string; active: boolean; cycle_count: number; max_cycles: number }[]>) {
      const next: Record<string, { cycle_count: number; max_cycles: number }> = {}
      for (const loop of action.payload) {
        if (!loop.active || isUnsafeKey(loop.slot)) continue
        next[safeKey(loop.slot)] = {
          cycle_count: Math.max(0, Math.floor(Number(loop.cycle_count) || 0)),
          max_cycles: Math.max(0, Math.floor(Number(loop.max_cycles) || 0)),
        }
      }
      state.goalLoops = next
    },
    /** Upsert (or drop) one loop from an `autonudge_state` WS event. */
    sseGoalLoop(state, action: PayloadAction<{ slot: string; active: boolean; cycle_count: number; max_cycles: number }>) {
      const { slot, active } = action.payload
      if (isUnsafeKey(slot)) return
      // Same partial-preloaded-state tolerance as subagentQueued above.
      state.goalLoops ??= {}
      if (!active) { delete state.goalLoops[safeKey(slot)]; return }
      state.goalLoops[safeKey(slot)] = {
        cycle_count: Math.max(0, Math.floor(Number(action.payload.cycle_count) || 0)),
        max_cycles: Math.max(0, Math.floor(Number(action.payload.max_cycles) || 0)),
      }
    },
    sseSubagentPending(state, action: PayloadAction<{ slot: string; id: string; task: string; approval_id: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const entry: SubagentActivity = {
        id: action.payload.id, task: action.payload.task, agent: '',
        status: 'pending', streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
        approval_id: action.payload.approval_id,
      }
      if (action.payload.slot !== state.activeSlot) {
        const c = state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }
        c.subagents[safeKey(action.payload.id)] = entry
        return
      }
      state.subagents[safeKey(action.payload.id)] = entry
    },
    markSubagentApproving(state, action: PayloadAction<{ id: string; approving: boolean }>) {
      if (isUnsafeKey(action.payload.id)) return
      const a = state.subagents[action.payload.id]
      if (a) { a.approving = action.payload.approving; return }
      for (const sa of Object.values(state.slotActivity)) {
        const b = sa.subagents[action.payload.id]
        if (b) { b.approving = action.payload.approving; return }
      }
    },
    sseSubagentSpawn(state, action: PayloadAction<{ slot: string; id: string; task: string; agent: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[action.payload.id]
      if (existing?.status === 'pending') {
        existing.status = 'running'
        existing.agent = action.payload.agent || existing.agent || 'kirocrew'
        // The spawn event carries the authoritative task text (the pending
        // card's task is derived from the approval title, which may be empty
        // or just "spawn_run") — always prefer the spawn payload's task.
        if (action.payload.task) existing.task = action.payload.task
        return
      }
      subs[safeKey(action.payload.id)] = {
        id: action.payload.id, task: action.payload.task, agent: action.payload.agent || 'kirocrew',
        status: 'running', streaming: existing?.streaming || '', lastTool: '', startedAt: existing?.startedAt || Date.now(), elapsed: 0,
        toolCount: 0, stalled: false,
      }
    },
    sseSubagentChunk(state, action: PayloadAction<{ slot: string; id: string; text: string }>) {
      // Prototype-pollution guard is centralized in getSlotSub (fail-closed on
      // __proto__/constructor/prototype ids) so no call site can forget it.
      const a = getSlotSub(state, action.payload.slot, action.payload.id)
      if (a) {
        a.retrying = false
        a.streaming += action.payload.text
        if (a.streaming.length > 50_000) {
          a.streaming = i18nT('store.chatSlice.truncated') + '\n' + a.streaming.slice(-40_000)
        }
      }
    },
    sseSubagentTool(state, action: PayloadAction<{ slot: string; id: string; tool: string; turns?: number; tool_count?: number }>) {
      const { slot, id } = action.payload
      // Prototype-pollution guard is centralized in getSlotSub.
      const a = getSlotSub(state, slot, id)
      if (a) {
        a.lastTool = action.payload.tool; a.status = 'tool'
        if (typeof action.payload.tool_count === 'number') a.toolCount = action.payload.tool_count
        a.stalled = false
        a.retrying = false
      }
    },
    sseSubagentRetrying(state, action: PayloadAction<{ slot: string; id: string; attempt?: number }>) {
      // Fired for both transient-backend retries (subagent_retrying) and the
      // one-shot cancel auto-continue (subagent_recovering): the agent is
      // still alive and recovering — show ⟳ instead of letting it look hung.
      const { slot, id } = action.payload
      if (id === '__proto__' || id === 'constructor' || id === 'prototype') return
      const a = getSlotSubs(state, slot)?.[id]
      if (a) { a.retrying = true; a.stalled = false }
    },
    sseSubagentStalled(state, action: PayloadAction<{ slot: string; id: string; stalled: boolean; idle_secs?: number }>) {
      const { slot, id } = action.payload
      // Prototype-pollution guard is centralized in getSlotSub.
      const a = getSlotSub(state, slot, id)
      if (a) a.stalled = action.payload.stalled
    },
    /** One coalesced ~1s frame carrying the latest delta per agent (scale
     *  plumbing — replaces per-event tool/stalled/retrying frames when many
     *  agents run). Field presence decides what to apply; latest wins. */
    sseSubagentBatchUpdate(state, action: PayloadAction<{ updates: { id: string; slot: string; tool?: string; tool_count?: number; stalled?: boolean; attempt?: number }[] }>) {
      for (const u of action.payload.updates || []) {
        const a = getSlotSub(state, u.slot, u.id)
        if (!a) continue
        // Order matters: retrying (attempt) applies FIRST so a tool field in
        // the same merged entry — meaning work resumed — clears it last.
        if (typeof u.attempt === 'number') { a.retrying = true; a.stalled = false }
        if (typeof u.tool === 'string' && u.tool) { a.lastTool = u.tool; if (a.status === 'running') a.status = 'tool'; a.retrying = false }
        if (typeof u.tool_count === 'number') a.toolCount = u.tool_count
        if (typeof u.stalled === 'boolean') a.stalled = u.stalled
      }
    },
    /** One coalesced ~1s frame of concatenated streaming text per agent
     *  (subscriber-only, mirrors sseSubagentChunk semantics). */
    sseSubagentBatchChunks(state, action: PayloadAction<{ chunks: { id: string; slot: string; text: string }[] }>) {
      for (const c of action.payload.chunks || []) {
        const a = getSlotSub(state, c.slot, c.id)
        if (!a) continue
        a.retrying = false
        a.streaming += c.text
        if (a.streaming.length > 50_000) {
          a.streaming = i18nT('store.chatSlice.truncated') + '\n' + a.streaming.slice(-40_000)
        }
      }
    },
    /** Chip row click → the Activity tab scrolls to/expands this agent. */
    selectSubagent(state, action: PayloadAction<string | null>) {
      state.selectedSubagentId = action.payload
    },
    /** "Dismiss done": drop terminal cards for a slot (backend clear is the
     *  caller's job via DELETE /api/spawn; this trims the local view). */
    clearTerminalSubagents(state, action: PayloadAction<{ slot: string }>) {
      const slot = action.payload.slot
      if (isUnsafeKey(slot)) return
      const subs = slot !== state.activeSlot
        ? state.slotActivity[safeKey(slot)]?.subagents
        : state.subagents
      if (!subs) return
      for (const id of Object.keys(subs)) {
        const st = subs[id]?.status
        if (st === 'done' || st === 'error' || st === 'stopped') delete subs[id]
      }
    },
    sseSubagentDone(state, action: PayloadAction<{ slot: string; id: string; elapsed: number; error?: string; stopped?: boolean; outcome?: 'completed' | 'failed' | 'stopped'; task?: string; agent?: string; result?: string }>) {
      if (isUnsafeKey(action.payload.slot) || isUnsafeKey(action.payload.id)) return
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      let a = subs[action.payload.id]
      if (!a) {
        // Cross-slot fallback: the card may live under a different slot key.
        if (state.subagents[action.payload.id]) a = state.subagents[action.payload.id]
        else {
          for (const sa of Object.values(state.slotActivity)) {
            if (sa.subagents[action.payload.id]) { a = sa.subagents[action.payload.id]; break }
          }
        }
      }
      const isNative = action.payload.id.startsWith('native:')
      // Canonical terminal classification: `outcome` is the single source
      // (spec: docs/system-specs/modules/subagent.md). `stopped`/`error`
      // derivation is kept ONLY as a fallback for old payloads that predate
      // the field (reconnect replays from a pre-upgrade gateway).
      const doneStatus: 'stopped' | 'error' | 'done' =
        action.payload.outcome === 'stopped' ? 'stopped'
          : action.payload.outcome === 'failed' ? 'error'
            : action.payload.outcome === 'completed' ? 'done'
              : action.payload.stopped ? 'stopped' : (action.payload.error ? 'error' : 'done')
      if (a) {
        a.status = doneStatus
        a.retrying = false
        a.elapsed = action.payload.elapsed
        a.error = doneStatus === 'stopped' ? undefined : action.payload.error
        a.streaming = ''
        if (action.payload.task && !a.task) a.task = action.payload.task
        if (action.payload.agent && !a.agent) a.agent = action.payload.agent
        if (isNative && action.payload.result !== undefined) a.result = action.payload.result
      }
      else {
        subs[action.payload.id] = {
          id: action.payload.id,
          task: action.payload.task || '',
          agent: action.payload.agent || 'kirocrew',
          status: doneStatus,
          streaming: '',
          lastTool: '',
          startedAt: Date.now() - action.payload.elapsed * 1000,
          elapsed: action.payload.elapsed,
          error: doneStatus === 'stopped' ? undefined : action.payload.error,
          result: isNative ? action.payload.result : undefined,
        }
      }
    },
    sseSideResult(state, action: PayloadAction<{ slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; is_error?: boolean; final?: boolean }>) {
      const { slot, run_id, role, content, ts, is_error, final } = action.payload
      if (isUnsafeKey(slot)) return
      const tsIso = typeof ts === 'number' ? new Date(ts * 1000).toISOString() : new Date().toISOString()
      // Intentional re-open (new user frame) clears the closed sentinel
      if (role === 'user' && state.slotSideClosed[slot]) {
        delete state.slotSideClosed[slot]
      }
      // Block late assistant chunks after sideClose
      if (!state.slotSide[slot] && state.slotSideClosed[slot]) return
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: tsIso }
      }
      const side: SideState = state.slotSide[slot]
      if (role === 'user') {
        // Reconcile with optimistic bubble appended in sideOptimisticAppend.
        const lastUser = side.messages[side.messages.length - 1]
        if (lastUser?.role === 'user' && lastUser.content === content && !lastUser.run_id) {
          lastUser.run_id = run_id
          lastUser.ts = tsIso
        } else {
          side.messages.push({ role: 'user', content, ts: tsIso, run_id })
        }
        side.lastRunId = run_id
        side.pending = true
        side.streaming = true
        return
      }
      side.pending = false
      side.streaming = !final
      if (is_error) {
        side.messages.push({ role: 'assistant', content, ts: tsIso, run_id, is_error: true })
        side.lastRunId = run_id
        return
      }
      const last = side.messages[side.messages.length - 1]
      if (last?.role === 'assistant' && last.run_id === run_id && !last.is_error) {
        if (content === last.content) return
        last.content = content.startsWith(last.content) ? content : last.content + content
        last.ts = tsIso
        return
      }
      side.messages.push({ role: 'assistant', content, ts: tsIso, run_id })
      side.lastRunId = run_id
    },
    sideClose(state, action: PayloadAction<string>) {
      delete state.slotSide[action.payload]
      if (isUnsafeKey(action.payload)) return
      state.slotSideClosed[safeKey(action.payload)] = true
    },
    sideOptimisticAppend(state, action: PayloadAction<{ slot: string; message: SideMessage }>) {
      const { slot, message } = action.payload
      if (isUnsafeKey(slot)) return
      if (state.slotSideClosed[slot]) delete state.slotSideClosed[slot]
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[safeKey(slot)] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: message.ts }
      }
      const side = state.slotSide[slot]
      side.messages.push(message)
      side.pending = true
    },
    sideOptimisticRollback(state, action: PayloadAction<string>) {
      const side = state.slotSide[action.payload]
      if (!side) return
      const last = side.messages[side.messages.length - 1]
      if (last?.role === 'user') side.messages.pop()
      side.pending = false
    },
    sseSubagentSnapshot(state, action: PayloadAction<{ id: string; slot: string; task: string; agent: string; streaming: string; last_tool: string; started: number; tool_count?: number; stalled?: boolean }>) {
      const d = action.payload
      if (isUnsafeKey(d.slot) || isUnsafeKey(d.id)) return
      const subs = d.slot && d.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(d.slot)] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[d.id]
      // Live events can interleave with replay because subscription starts before
      // snapshots are sent. Never let a stale running snapshot demote a terminal card.
      if (existing?.status === 'done' || existing?.status === 'error') return
      subs[safeKey(d.id)] = {
        id: d.id, task: d.task, agent: d.agent || 'kirocrew',
        status: d.last_tool ? 'tool' : 'running', streaming: d.streaming, lastTool: d.last_tool,
        startedAt: d.started * 1000, elapsed: 0,
        toolCount: d.tool_count ?? 0, stalled: d.stalled ?? false,
        approval_id: existing?.approval_id, approving: existing?.approving,
      }
    },
    /** Fold a single dynamic-workflow run event into workflowRuns. */
    sseWorkflowEvent(state, action: PayloadAction<{ run_id: string; session_key?: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }>) {
      const { run_id, type, data, session_key } = action.payload
      if (isUnsafeKey(run_id)) return
      if (!run_id) return
      const d = (data || {}) as Record<string, unknown>
      const cur = state.workflowRuns[run_id] ?? {
        run_id, name: '', phase: '', lastLog: '', status: 'running' as const,
      }
      if (session_key && !cur.sessionKey) cur.sessionKey = session_key
      switch (type) {
        case 'run_started':
          cur.name = (d.name as string) || cur.name || run_id
          cur.status = 'running'
          break
        case 'phase_started':
          cur.phase = (d.title as string) || cur.phase
          break
        case 'log': {
          const msg = (d.message as string) || ''
          if (msg) cur.lastLog = msg
          break
        }
        case 'run_finished':
          cur.status = 'finished'
          break
        case 'run_failed':
          cur.status = 'failed'
          cur.error = (d.error as string) || cur.error
          break
        case 'run_cancelled':
          cur.status = 'cancelled'
          break
        default:
          break
      }
      state.workflowRuns[safeKey(run_id)] = cur
    },
    clearWorkflowRun(state, action: PayloadAction<string>) {
      delete state.workflowRuns[action.payload]
    },
    sseChatMessageUpdate(state, action: PayloadAction<{ slot: string; tool_call_id?: string; ts?: string; content?: string; meta?: Record<string, unknown> }>) {
      const { slot, tool_call_id: tcid, ts, content, meta } = action.payload
      if (!slot) return

      if (tcid) {
        const updateByTcid = (msgs: ChatMessage[]) => {
          for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i]
            const mMeta = m.meta as Record<string, unknown> | undefined
            if (m.role === 'tool' && mMeta?.tool_call_id === tcid) {
              if (content !== undefined) m.content = content
              if (meta) m.meta = { ...(mMeta || {}), ...meta }
              break
            }
          }
        }
        if (slot === state.activeSlot) updateByTcid(state.messages)
        const cached = state.slotMessages[slot]
        if (cached) updateByTcid(cached)
      } else if (ts) {
        const apply = (msgs: ChatMessage[]) => {
          const idx = msgs.findIndex(m => m.ts === ts)
          if (idx < 0) return
          const target = msgs[idx]
          if (meta) target.meta = { ...(target.meta || {}), ...meta }
          if (content !== undefined) target.content = content
        }
        if (slot === state.activeSlot) apply(state.messages)
        const cached = state.slotMessages[slot]
        if (cached) apply(cached)
      }
    },
    sseToolActivity(state, action: PayloadAction<{ slot: string; tool: string; kind: string; purpose: string; input_preview: string; auto?: boolean; tool_call_id?: string; is_update?: boolean; is_shell?: boolean }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).toolLog
        : state.toolLog
      // claude-agent-acp emits an initial tool_call with empty rawInput followed
      // by tool_call_update notifications carrying the populated payload. The
      // backend sets is_update:true on the second-phase event so we merge into
      // the existing entry by tool_call_id. We gate strictly on is_update to
      // avoid silently merging a replayed initial event (e.g. WebSocket
      // reconnect) into an unrelated tool with a colliding id.
      const tcid = action.payload.tool_call_id
      if (tcid && action.payload.is_update) {
        const existing = log.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
        if (existing) {
          if (action.payload.tool) existing.text = action.payload.tool
          if (action.payload.purpose) existing.purpose = action.payload.purpose
          if (action.payload.input_preview) existing.input = action.payload.input_preview
          if (action.payload.kind) existing.kind = action.payload.kind
          if (action.payload.is_shell !== undefined) existing.is_shell = action.payload.is_shell
          existing.ts = Date.now()
          return
        }
      }
      log.push({ type: 'tool', text: action.payload.tool, purpose: action.payload.purpose, input: action.payload.input_preview, kind: action.payload.kind, ts: Date.now(), auto: action.payload.auto, tool_call_id: action.payload.tool_call_id, is_shell: action.payload.is_shell })
      if (log.length > 100) log.splice(0, log.length - 100)
    },
    sseActivityEvent(state, action: PayloadAction<{ slot: string; kind: string; text: string; approval_id?: string; approval_type?: string }>) {
      if (isUnsafeKey(action.payload.slot)) return
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[safeKey(action.payload.slot)] ??= { toolLog: [], subagents: {} }).toolLog
        : state.toolLog
      if (action.payload.kind === 'approval_resolved') {
        const id = action.payload.approval_id
        const entry = log.find(e => e.type === 'approval' && e.approval_id === id)
        if (entry) entry.type = 'approval_resolved'
        // Also mark the permission message as resolved so ApprovalBar hides it
        const msg = state.messages.findLast(m => m.role === 'permission' && (m.meta as Record<string,unknown>)?.approval_id === id)
        if (msg && !(msg.meta as Record<string,unknown>).resolved) (msg.meta as Record<string,unknown>).resolved = 'approved'
        return
      }
      const entry: ToolActivity = { type: action.payload.kind, text: action.payload.text, ts: Date.now() }
      if (action.payload.approval_id) entry.approval_id = action.payload.approval_id
      if (action.payload.approval_type) entry.approval_type = action.payload.approval_type
      log.push(entry)
    },
    sseToolResult(state, action: PayloadAction<{ slot: string; output: string; tool_call_id?: string }>) {
      const tid = action.payload.tool_call_id
      // Land the output on the tool MESSAGE's meta as well as the tool log, for
      // the one consumer that reads scrollback rather than the tool log: the
      // inline SubagentRunCard detects a spawn_run launch by parsing
      // "Spawned N subagent(s)." out of `meta.output`. Without this the card
      // sees nothing until the slot is refetched, since `chatSlotDetail` would
      // be the only source carrying this field — a reload-only artifact. Mirrors
      // the server, which writes the same redacted string to the same field
      // (chat_runner.py EVENT_TOOL_RESULT), so live and reloaded state agree.
      //
      // Restricted to launch results on purpose. `toolLog` is capped at 100
      // entries but `state.messages` is not, and a single output can reach the
      // server's 1 MB cap, so copying EVERY tool result here would let one long
      // autonomous turn grow the heap without bound.
      //
      // Runs BEFORE the tool-log lookup below, which returns early for a slot
      // that has no toolLog yet — a background slot's scrollback still needs
      // the output.
      //
      // Only with an explicit tool_call_id: the id-less fallback below is safe
      // for the tool log (positional, single-writer) but would attach output
      // to an arbitrary tool bubble in scrollback. The server applies the same
      // condition (`if _tcid:`), so skipping is parity, not a gap.
      if (tid && action.payload.output.includes(SPAWN_LAUNCH_MARKER)) {
        applyToolOutputToMessages(state, action.payload.slot, tid, action.payload.output)
      }
      const log = action.payload.slot !== state.activeSlot
        ? state.slotActivity[action.payload.slot]?.toolLog
        : state.toolLog
      if (!log) return
      // Prefer an exact tool_call_id match when a tid is supplied. Only if no
      // entry carries that id do we fall back to the most-recent id-less tool
      // entry. A single-pass `... || !log[i].tool_call_id` clause would let a
      // supplied tid latch onto an unrelated id-less tool sitting later in the
      // log, attaching the output to the wrong tool bubble.
      let target = -1
      if (tid) {
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && log[i].tool_call_id === tid) { target = i; break }
        }
      }
      if (target === -1) {
        for (let i = log.length - 1; i >= 0; i--) {
          if (log[i].type === 'tool' && (!tid || !log[i].tool_call_id)) { target = i; break }
        }
      }
      if (target >= 0) log[target].output = action.payload.output
    },
    /** Store an MCP App (SEP-1865) render payload, keyed by BOTH its session
     *  and tool_call_id (see mcpAppKey): the session scope means an ACP
     *  tool-call-id reuse across slots can never cross-render another
     *  session's app (or its live callback capability), and per-slot eviction
     *  (payloads are multi-MB) is a simple prefix scan. */
    sseMcpAppRender(state, action: PayloadAction<McpAppRenderPayload>) {
      const p = action.payload
      if (!p?.tool_call_id || isUnsafeKey(p.tool_call_id)) return
      if (!p.session_key || isUnsafeKey(p.session_key)) return
      state.mcpApps[mcpAppKey(p.session_key, p.tool_call_id)] = p
      // Bound per-slot retention: payloads carry multi-MB app HTML, so a
      // long-lived slot that renders many apps must not grow unbounded. Keys
      // enumerate in insertion order, so the oldest slot entries are dropped
      // first once the cap is exceeded.
      const prefix = `${p.session_key}\u001F`
      const slotKeys = Object.keys(state.mcpApps).filter((k) => k.startsWith(prefix))
      for (let i = 0; i < slotKeys.length - MCP_APPS_PER_SLOT_MAX; i++) {
        delete state.mcpApps[slotKeys[i]]
      }
    },
    /** Handle chat messages pushed via global SSE/WS (works after refresh). */
    /** Accumulate streamed model reasoning (`chat_thinking` WS event) into a
     *  single content-bearing `thinking`-role message for the current turn.
     *  Reasoning normally arrives before the visible answer, so the block sits
     *  above the streamed assistant text. Scans back to the turn boundary (the
     *  last user message) to keep one reasoning block per turn. */
    sseThinkingChunk(state, action: PayloadAction<{ slot: string; content: string }>) {
      const { slot, content } = action.payload
      if (slot !== state.activeSlot || !content) return
      for (let i = state.messages.length - 1; i >= 0; i--) {
        if (state.messages[i].role === 'thinking') { state.messages[i].content += content; return }
        if (state.messages[i].role === 'user') break
      }
      state.messages.push({ role: 'thinking', content, cls: '', meta: { clientTs: mintMsgId() } })
    },
    sseChatMessage(state, action: PayloadAction<{ slot: string; role: string; content: string; ts?: string; seq?: number; cls?: string; meta?: Record<string, unknown>; kind?: string; batched?: boolean }>) {
      const { slot, role, content, ts, seq, cls, meta, kind, batched } = action.payload
      if (slot !== state.activeSlot) { applyNonActiveFrame(state, action.payload); return }
      // stop_event — replace in place by id, or insert new
      const effectiveKind = kind ?? (meta?.kind as string | undefined)
      if (effectiveKind === 'stop_event') {
        const id = (meta?.id as string) ?? ''
        const idx = id ? state.messages.findIndex(m => m.meta?.id === id) : -1
        const msg: ChatMessage = ensureMsgId({ role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' })
        if (idx >= 0) { state.messages[idx] = msg } else { state.messages.push(msg) }
        return
      }
      // WS segment — finalize streaming into assistant without resetting sequence or slot state
      if (role === '_segment') {
        finalizeTrailingStreaming(state.messages)
        return
      }
      // WS chunk — accumulate into streaming message, preserve rawText
      if (role === 'chunk') {
        state.slotState = 'streaming'
        state._wsChunkedDuringFetch = true
        // Drop only the empty "Thinking…" placeholder; keep content-bearing
        // reasoning blocks (from chat_thinking) so they persist as a collapsible
        // trace directly above the streamed answer.
        if (state.messages.some(m => m.role === 'thinking' && !m.content)) {
          state.messages = state.messages.filter(m => !(m.role === 'thinking' && !m.content))
        }
        // Accumulate reasoning text into activity timeline
        const last = state.toolLog[state.toolLog.length - 1]
        if (last?.type === 'reasoning') {
          last.text += content
        } else {
          state.toolLog.push({ type: 'reasoning', text: content, ts: Date.now() })
        }
        let streamIdx = -1
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') { streamIdx = i; break }
        }
        if (streamIdx >= 0) {
          const msg = state.messages[streamIdx]
          // Defensive non-batched gap detection. The live WS path always sets
          // `batched` — the useWebSocket flush buffer owns gap detection across
          // the chunks it merges and inlines the marker itself — so this branch
          // only runs for a direct (test/legacy) non-batched chunk dispatch. It
          // shares missedChunkMarker with the buffer so the two cannot drift.
          if (!batched && seq !== undefined && state.lastChunkSeq !== undefined) {
            msg.content += missedChunkMarker(state.lastChunkSeq, seq)
          }
          msg.content += content
          msg.rawText = msg.content
        } else {
          state.messages.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content, meta: { clientTs: mintMsgId() } })
        }
        if (seq !== undefined) state.lastChunkSeq = seq
        return
      }
      // WS done — finalize streaming into assistant, rawText preserved for reparse
      if (role === '_done') {
        state.slotState = 'idle'
        state.lastChunkSeq = undefined
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            const msg = state.messages[i]
            msg.role = 'assistant'
            msg.rawText = msg.content
            break
          }
        }
        state.slotRunning = false
        state.slotStopping = false
        state.slotState = 'idle'
        state.pendingTurnSlot = null
        return
      }
      // Compacting — block input, show footer indicator (no visible message)
      if (role === 'compacting') {
        if (action.payload.slot && action.payload.slot !== state.activeSlot) return
        state.slotState = 'compacting'
        state.slotRunning = true
        return
      }
      // Permission messages carry request_id/tool_input in cls (JSON) — lift into
      // meta here, BEFORE the guard, so the identity comparison sees the same
      // `tool_call_id` the stored row has.
      let effectiveMeta = meta
      if (role === 'permission' && !meta?.approval_id && cls) {
        try {
          const parsed = JSON.parse(cls)
          if (parsed.request_id) {
            effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
          }
        } catch { /* not JSON cls, ignore */ }
      }
      // If this permission's tool was already rejected/stopped, mark it resolved immediately
      if (role === 'permission') {
        const tcid = (effectiveMeta?.tool_call_id as string) || ''
        if (tcid) {
          const entry = state.toolLog.findLast(e => e.type === 'tool' && e.tool_call_id === tcid)
          if (entry?.rejected) effectiveMeta = { ...effectiveMeta, resolved: 'rejected' }
        }
      }
      // Idempotent append — ONE chokepoint that dominates every branch below,
      // which is the point: each of those branches creates or MUTATES a row and
      // returns, so a guard placed after any of them is a guard some frame slips
      // past. The `assistant` branch is the sharpest case: it overwrites the
      // trailing `streaming` row, so a late redelivery of an OLD assistant frame
      // would clobber the live content of a NEW segment already streaming.
      if (isRedeliveredMessage(state.messages, effectiveMeta)) { state._redeliveredFramesDropped += 1; return }
      // Tool call — update state, insert before streaming message
      if (role === 'tool') {
        state.slotState = 'tool_running'
        // Insert tool before any trailing streaming message so
        // chat_segment can still find and finalize it with redacted text.
        let insertIdx = state.messages.length
        if (insertIdx > 0 && state.messages[insertIdx - 1]?.role === 'streaming') {
          insertIdx--
        }
        state.messages.splice(insertIdx, 0, ensureMsgId({ role, content, cls: cls || '', ts, meta }))
        return
      }
      // Thinking — deduplicate, only keep one
      if (role === 'thinking') {
        if (state.messages.some(m => m.role === 'thinking')) return
        state.messages.push({ role: 'thinking', content: '', cls: '', meta: { clientTs: mintMsgId() } })
        return
      }
      // Replace streaming placeholder with final assistant message
      if (role === 'assistant') {
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            state.messages[i].role = 'assistant'; state.messages[i].content = content; if (ts) state.messages[i].ts = ts
            // Carry the frame's meta — crucially `mid`, this row's server
            // identity. The row was minted client-side by the first `chunk` and
            // has none until now; without it a later redelivery of THIS frame is
            // unrecognisable and would overwrite whatever is streaming then.
            if (meta) state.messages[i].meta = { ...(state.messages[i].meta || {}), ...meta }
            return
          }
        }
      }
      // New user message = new turn — clear activity log
      if (role === 'user') {
        // A steered message does not start a new turn — skip the "stale permissions"
        // cleanup so the approval bar remains visible and answerable (#1667).
        if (!meta?.steer) {
          state.toolLog = []
          // Auto-resolve any stale permissions from previous turn so they don't block the new turn
          for (const m of state.messages) {
            if (m.role === 'permission' && !m.meta?.resolved) {
              if (m.meta) m.meta.resolved = 'rejected'
              else m.meta = { resolved: 'rejected' }
            }
          }
        }
      }
      state.messages.push(ensureMsgId({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind }))
    },
    /** Patch an existing message identified by ts. Used by the `chat_message_update`
     * server event to flip an mcp_oauth banner from "needs auth" to "authenticated"
     * after kiro-cli emits server_initialized. Patches both the active messages
     * array and the slotMessages cache so a slot the user isn't currently
     * viewing still shows the correct banner state on switch-back. */
    sseChatMessagePatchByTs(state, action: PayloadAction<{ slot: string; ts: string; meta?: Record<string, unknown>; content?: string }>) {
      const { slot, ts, meta, content } = action.payload
      if (!slot || !ts) return
      const apply = (msgs: ChatMessage[]) => {
        const idx = msgs.findIndex(m => m.ts === ts)
        if (idx < 0) return
        const target = msgs[idx]
        if (meta) target.meta = { ...(target.meta || {}), ...meta }
        if (content !== undefined) target.content = content
      }
      if (slot === state.activeSlot) apply(state.messages)
      const cached = state.slotMessages[slot]
      if (cached) apply(cached)
    },
    /** Remove the first queued message matching content and append a user bubble at the end. */
    removeQueuedMessage(state, action: PayloadAction<{ slot: string; content: string; queue_id?: string }>) {
      const { slot, content, queue_id } = action.payload
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = queue_id
        ? msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
        : msgs.findIndex(m => m.role === 'queued' && m.content === content)
      if (idx >= 0) {
        const ts = msgs[idx].ts
        msgs.splice(idx, 1)
        msgs.push({ role: 'user', content, cls: 'msg msg-u', ts })
      }
    },
    /** Cancel a queued message: remove from messages. pendingInput is set locally by the initiating client. */
    cancelQueuedMessage(state, action: PayloadAction<{ slot: string; queue_id: string }>) {
      const { slot, queue_id } = action.payload
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      if (idx >= 0) msgs.splice(idx, 1)
    },
    /** Edit a queued message in place (from backend queue_edit WS event or optimistic local update). */
    editQueuedMessage(state, action: PayloadAction<{ slot: string; queue_id: string; content: string }>) {
      const { slot, queue_id, content } = action.payload
      if (isUnsafeKey(slot)) return
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      if (idx >= 0) msgs[idx].content = content
    },
    /** Add a queued message (from backend queue_push WS event). */
    appendQueuedMessage: {
      reducer(state, action: PayloadAction<{ slot: string; content: string; ts: string; queueId: string }>) {
        const { slot, content, ts, queueId } = action.payload
        const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[safeKey(slot)] ??= [])
        msgs.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId } })
      },
      prepare(payload: { slot: string; content: string; ts: string; queue_id?: string }) {
        return { payload: { ...payload, queueId: payload.queue_id || crypto.randomUUID() } }
      },
    },
  },
  extraReducers: (builder) => {
    builder
      /** Reconcile per-slot caches against the authoritative slots list.
       *  Sessions that close/archive/delete vanish from the SSE `slots` REPLACE;
       *  without this reconcile their transcripts stay resident for the tab's
       *  lifetime (only `deleteSlot.fulfilled` evicts) — the dominant retention
       *  class behind multi-GB heaps on long-lived dashboard tabs.
       *  Guards: an empty payload is a no-op (SSE reconnect can deliver an
       *  empty frame before the first real snapshot), and the active slot is
       *  never pruned (its live `messages`/optimistic state must not be
       *  dropped out from under the open pane). `subagents`/`workflowRuns`
       *  are intentionally excluded — different keyspaces (dashboard:<slot>,
       *  run id), not bare slot keys. */
      .addCase(sseSlots, (state, action) => {
        if (action.payload.length === 0) return
        const live = new Set(action.payload.map(s => s.key))
        if (state.activeSlot) live.add(state.activeSlot)
        const maps = [
          state.slotMessages, state.slotActivity, state.slotRun, state.slotHydrated,
          state.slotSide, state.slotSideClosed, state.slotStatusDetail,
          state.slotContextPct, state.slotContextTokens, state.stopPressedAt,
          // Follow-up cards are per slot and can hold multi-KB prompts, so a
          // deleted session's card must not outlive it.
          state.followups,
          state.folderSuggestions,
        ].filter(Boolean)
        const cached = new Set(maps.flatMap(m => Object.keys(m)))
        for (const key of cached) {
          if (live.has(key)) continue
          for (const m of maps) delete m[key]
        }
        state.slotHistory = (state.slotHistory ?? []).filter(k => live.has(k))
      })
      .addCase(fetchHistory.fulfilled, (state, action) => {
        const { sessions, hasMore, offset, append } = action.payload
        state.history = append ? [...state.history, ...sessions] : sessions
        state.historyHasMore = hasMore
        state.historyOffset = offset + sessions.length
      })
      .addCase(switchSlot.pending, (state, action) => {
        // Save current slot's activity
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
        }
        // Cache current slot's messages before switching
        if (state.activeSlot && state.messages.length > 0) {
          state.slotMessages[state.activeSlot] = state.messages
        }
        // Always strip target from history: activeSlot ∉ slotHistory
        state.slotHistory = state.slotHistory.filter(k => k !== action.meta.arg)
        if (state.activeSlot && state.activeSlot !== action.meta.arg) {
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        // Restore target slot's activity (or empty)
        const cached = state.slotActivity[action.meta.arg]
        state.toolLog = cached?.toolLog ?? []
        state.subagents = cached?.subagents ?? {}
        // Inline expansion replaces the old 'tools' tab; cached pre-migration
        // values fall back to 'files'.
        state.activityTab = (cached?.activityTab && cached.activityTab !== ('tools' as never) && cached.activityTab !== ('nav' as never)) ? cached.activityTab : 'files'
        // Panel open/closed is per-chat; a chat we've never opened defaults to closed.
        state.activityOpen = cached?.activityOpen ?? false
        // Set activeSlot immediately so WS events for the new slot are accepted.
        // Restore cached messages if available (instant switch), otherwise show loading.
        state.activeSlot = action.meta.arg
        const cachedMsgs = state.slotMessages[action.meta.arg]
        if (cachedMsgs) {
          state.messages = cachedMsgs
          state.slotLoading = false
        } else {
          state.messages = []
          state.slotLoading = true
        }
        state._wsChunkedDuringFetch = false
      })
      .addCase(switchSlot.fulfilled, (state, action) => {
        const { key, messages, running, hasMore, total, queue } = action.payload
        if (isUnsafeKey(key)) return
        if (state.activeSlot !== key) return  // user switched away during fetch
        state.slotState = running ? 'streaming' : 'idle'
        // Mark stale permissions as resolved so ApprovalBar ignores them
        if (!running) {
          for (const m of messages) {
            if (m.role === 'permission' && !m.meta?.resolved) m.meta = { ...m.meta, resolved: 'stale' }
          }
        }
        // If WS already delivered newer streaming content, append it to fetched messages
        const lastLocal = state.messages[state.messages.length - 1]
        const preserved = mergePreservedPastes(state.messages, messages)
        // Does the fetched history already contain the local trailing reply?
        // The server row id answers it exactly, so when the local reply HAS one
        // that is the only test — falling back to content as well would let a
        // stale snapshot row with identical text (a different row, different id)
        // match and drop the newest reply. Content equality is only for a reply
        // that has no id yet: streamed in this session and never reloaded, so the
        // server history cannot hold it under a different id anyway.
        //
        // Preferring the id also survives the redaction asymmetry: this endpoint
        // redacts on emit (chat_utils._prepare_messages) while the streamed copy
        // is raw, so one row legitimately arrives with different bytes.
        const localMid = lastLocal?.meta?.mid
        const serverHasLastLocal = !!lastLocal && (
          typeof localMid === 'string' && !!localMid
            ? preserved.some(m => m.role === 'assistant' && m.meta?.mid === localMid)
            : preserved.some(m => m.role === 'assistant' && m.content === lastLocal.content)
        )
        if (
          state._wsChunkedDuringFetch
          && lastLocal?.role === 'streaming'
          && lastLocal.content.length > 0
        ) {
          // WS chunks arrived during fetch — use fetched history + local streaming
          state.messages = [...preserved.filter(m => m.role !== 'streaming'), lastLocal]
        } else if (
          lastLocal
          && (lastLocal.role === 'assistant' || lastLocal.role === 'streaming')
          && !!lastLocal.content && lastLocal.content.length > 0
          && !serverHasLastLocal
        ) {
          // The HTTP fetch resolved with a history that predates the reply we
          // already finalized locally (via applyNonActiveFrame while this slot
          // was backgrounded). Blindly replacing with the server response here
          // is the "switch away and back drops the latest response" regression.
          // Keep the server history but re-attach the local trailing reply.
          // Guarded by serverHasLastLocal above (row id, else exact content) so
          // we never duplicate a reply the server already returned, and never
          // drop a genuinely newer one: a different row has a different id, and
          // the content fallback stays EXACT rather than fuzzy.
          //
          // Only finalize a still-'streaming' partial to 'assistant' when the
          // turn is NOT still running. If the slot is still streaming
          // (running=true — e.g. switching back to a background slot whose
          // reply is mid-flight), coercing to 'assistant' freezes the partial:
          // the resuming `chunk` handler finds no trailing 'streaming' message
          // and pushes a NEW one, splitting the single reply across two bubbles
          // until chat_done heals it. Keep it 'streaming' so the stream resumes
          // into the same bubble.
          const finalized: ChatMessage = (lastLocal.role === 'streaming' && !running)
            ? { ...lastLocal, role: 'assistant' }
            : lastLocal
          state.messages = [...preserved.filter(m => m.role !== 'streaming'), finalized]
        } else {
          state.messages = preserved
        }
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        state.slotHasMore = hasMore
        state.slotOldestIndex = hasMore ? total - messages.length : 0
        // Hydrate queued messages from the backend queue field through the
        // single shared path (hydrateQueuedBubbles) so this reducer cannot drift
        // from warmSlotCache/refreshSlot. It strips any WS-delivered queued
        // bubbles first (a queue_push may have arrived during the fetch) so the
        // server queue set stays canonical and non-duplicated.
        state.messages = hydrateQueuedBubbles(state.messages, queue)
        // Update cache and clear loading state
        state.slotMessages[safeKey(key)] = state.messages
        state.slotLoading = false
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(switchSlot.rejected, (state, action) => {
        if (state.activeSlot !== action.meta.arg) return
        state.messages = []
        state.slotRunning = false
        state.slotStopping = false
        state.slotHasMore = false
        state.slotOldestIndex = 0
        state.slotLoading = false
      })
      .addCase(refreshSlot.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages, running, hasMore, total, queue } = action.payload
        if (isUnsafeKey(key)) return
        if (state.activeSlot !== key) return  // user switched away
        // Merge permission messages: prefer state perms (have frontend resolved flags)
        // but include API perms for any we don't have locally (e.g. arrived while disconnected)
        const statePerms = new Map<string, typeof state.messages[0]>()
        for (const m of state.messages) {
          if (m.role === 'permission' && m.meta?.approval_id) statePerms.set(m.meta.approval_id as string, m)
        }
        const apiPerms = messages.filter(m => m.role === 'permission')
        for (const m of apiPerms) {
          const aid = m.meta?.approval_id as string | undefined
          if (aid && !statePerms.has(aid)) statePerms.set(aid, m)
        }
        const tsNum = (v: unknown): number => {
          const s = v == null ? '' : String(v)
          if (!s) return 0
          const n = Number(s)
          if (Number.isFinite(n)) return n  // numeric epoch
          const p = Date.parse(s)
          return Number.isFinite(p) ? p / 1000 : 0  // ISO → epoch seconds
        }
        const merged = [...messages.filter(m => m.role !== 'permission'), ...statePerms.values()]
        const mergedWithPastes = mergePreservedPastes(state.messages, merged)
        // Only sort if permissions were re-injected (they need positional merge).
        // Backend messages arrive in order; sorting with mixed ts formats reorders them.
        const sorted = statePerms.size > 0
          ? mergedWithPastes.sort((a, b) => tsNum(a.ts) - tsNum(b.ts))
          : mergedWithPastes
        // Reasoning is client-only (never persisted server-side); re-insert it so
        // a finished turn's thinking block survives this refresh.
        state.messages = mergePreservedThinking(state.messages, mergePreservedClientTs(state.messages, sorted))
        // Re-hydrate queued bubbles through the SAME shared path as
        // switchSlot/warmSlotCache. The merge above is rebuilt from server
        // history + preserved perms/thinking and carries no `queued` bubbles, so
        // without this a refresh (e.g. the one fired on chat_done) would vanish a
        // user's pending queued messages. Routing all three slot-detail reducers
        // through hydrateQueuedBubbles is what stops them drifting apart again.
        state.messages = hydrateQueuedBubbles(state.messages, queue)
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        state.slotHasMore = hasMore
        state.slotOldestIndex = hasMore ? total - messages.length : 0
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(warmSlotCache.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages, queue } = action.payload
        if (isUnsafeKey(key)) return
        // Slot became active between dispatch and fulfilment — switchSlot now
        // owns its messages, so leave the cache for it to manage.
        if (state.activeSlot === key) return
        if (!state.slotMessages) state.slotMessages = {}
        // Preserve permission flags resolved client-side but not yet reflected
        // in the refetched history (a grid pane can resolve an approval between
        // the server snapshot and this warm), then collapse the pane's
        // optimistic/streamed/echoed messages to the canonical history.
        const localResolved = new Map<string, unknown>()
        for (const m of (state.slotMessages[key] || [])) {
          if (m.role === 'permission' && m.meta?.approval_id && m.meta?.resolved) {
            localResolved.set(m.meta.approval_id as string, m.meta.resolved)
          }
        }
        const hydrated = messages.map(m => {
          const aid = m.role === 'permission' ? (m.meta?.approval_id as string | undefined) : undefined
          return aid && localResolved.has(aid)
            ? { ...m, meta: { ...m.meta, resolved: localResolved.get(aid) } }
            : m
        })
        // Hydrate queued bubbles through the single shared path
        // (hydrateQueuedBubbles). Without this, warming a background slot's cache
        // dropped its pending queued bubbles, so switching to that slot rendered
        // the completed history minus anything the user had queued behind the
        // in-flight turn (the bubbles only reappeared on a later full fetch).
        // Routing every slot-detail reducer through the one helper is what keeps
        // this from silently diverging from switchSlot/refreshSlot again.
        state.slotMessages[safeKey(key)] = hydrateQueuedBubbles(hydrated, queue)
        // Clear the per-slot run indicator (the _done frame already idles it;
        // this is belt-and-braces for the fetch-completes-after-_done ordering).
        const run = (state.slotRun[safeKey(key)] ??= { state: 'idle' })
        run.state = 'idle'
        run.lastChunkSeq = undefined
        seedContextUsage(state, key, action.payload.context)
      })
      .addCase(createSlot.pending, (state) => { state.creatingSlot = true })
      .addCase(createSlot.rejected, (state) => { state.creatingSlot = false })
      .addCase(createSlot.fulfilled, (state, action) => {
        // The create POST resolved, so clear the pending flag regardless of
        // whether we activate below. Otherwise the switched-away early-return
        // would strand the "Creating…" spinner on forever.
        state.creatingSlot = false
        // Switched-away guard: if the user moved to a different
        // session while this create was pending (a slow "Creating…" under memory
        // pressure), do NOT hijack the view. The new slot is already registered
        // via addSlotOptimistic; just leave the user where they are. Mirrors the
        // guard switchSlot/refreshSlot/warmSlotCache already have. `send()`'s
        // forceNew path and welcome-screen New Chat both leave activeSlot equal
        // to the origin, so they still activate normally.
        //
        // Conscious edge: a rapid double New Chat from the same slot makes both
        // creates capture the same origin; the first fulfilled activates its
        // slot (moving activeSlot), so the second sees activeSlot !== origin and
        // stays put. "First create wins" rather than the prior "last wins". Both
        // slots exist in the sidebar and both land the user on an empty chat, so
        // the outcomes are equivalent, accepted over re-stealing focus.
        // Caller asked for a background create (see `activate` above): the slot
        // is registered but focus stays put until the caller switches to it.
        if (action.meta.activate === false) return
        const origin = action.meta.originActiveSlot ?? null
        if (state.activeSlot !== origin) return
        if (state.activeSlot) {
          state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
          state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
        }
        state.activeSlot = action.payload.key
        state.messages = []
        state.toolLog = []
        state.subagents = {}
        state.activityTab = 'files'
        state.slotRunning = false
        state.slotStopping = false
        state.slotState = 'idle'
        state.slotHasMore = false
        state.slotOldestIndex = 0
      })
      .addCase(deleteSlot.fulfilled, (state, action) => {
        delete state.slotActivity[action.payload]
        delete state.slotMessages[action.payload]
        delete state.slotRun[action.payload]
        delete state.slotHydrated[action.payload]
        delete state.slotSide[action.payload]
        delete state.slotSideClosed[action.payload]
        if (state.followups) delete state.followups[action.payload]
        if (state.folderSuggestions) delete state.folderSuggestions[action.payload]
        evictMcpApps(state, action.payload)
        state.slotHistory = state.slotHistory.filter(k => k !== action.payload)
        if (state.activeSlot === action.payload) {
          state.activeSlot = null
          state.messages = []
          state.toolLog = []
          state.subagents = {}
        }
      })
      .addCase(resumeFromHistory.fulfilled, (state, action) => {
        if (action.payload.ok) {
          state.slotHistory = state.slotHistory.filter(k => k !== action.payload.key)
          if (state.activeSlot) {
            state.slotActivity[state.activeSlot] = { toolLog: state.toolLog, subagents: state.subagents, activityTab: state.activityTab, activityOpen: state.activityOpen }
            if (state.activeSlot !== action.payload.key) {
              state.slotHistory = pushHistory(state.slotHistory, state.activeSlot)
            }
          }
          const cached = state.slotActivity[action.payload.key]
          state.toolLog = cached?.toolLog ?? []
          state.subagents = cached?.subagents ?? {}
          // Inline expansion replaces the old 'tools' tab; cached pre-migration values fall back to 'files'.
          state.activityTab = (cached?.activityTab && cached.activityTab !== ('tools' as never) && cached.activityTab !== ('nav' as never)) ? cached.activityTab : 'files'
          state.activityOpen = cached?.activityOpen ?? false
          state.activeSlot = action.payload.key
          state.messages = mergePreservedPastes(state.messages, action.payload.messages)
          state.slotState = 'idle'
          state.pendingTurnSlot = null
          state.slotHasMore = action.payload.hasMore
          state.slotOldestIndex = action.payload.hasMore ? action.payload.total - action.payload.messages.length : 0
        }
      })
      .addCase(deleteHistorySession.fulfilled, (state, action) => {
        state.history = state.history.filter(s => s.key !== action.payload)
      })
      .addCase(loadOlderMessages.pending, (state) => {
        state.loadingOlder = true
      })
      .addCase(loadOlderMessages.fulfilled, (state, action) => {
        state.loadingOlder = false
        if (action.payload) {
          // Merge paste state into the older messages first, then prepend so
          // historical pastes re-tokenize from localStorage instead of showing
          // as fully-expanded text.
          const merged = mergePreservedPastes(state.messages, action.payload.messages)
          state.messages = [...merged, ...state.messages]
          state.slotHasMore = action.payload.hasMore
          state.slotOldestIndex = action.payload.hasMore ? action.payload.total - state.messages.length : 0
        }
      })
      .addCase(loadOlderMessages.rejected, (state) => {
        state.loadingOlder = false
      })
  },
})

export const {
  setActiveSlot, clearSlotState, setPendingInput, setQuestionCard, clearQuestionCard, resolveQuestionCard, setFollowupCard, clearFollowupCard, dismissFollowupItem, setFolderSuggestion, clearFolderSuggestion, appendMessage, appendSlotMessage, updateStreamingMessage, finalizeAssistant,
  removeThinking, removeByApprovalId, resolveByApprovalId, clearPendingPermissions, setSlotRunning, setSlotStopping, startLocalTurn, syncSlotRunningFromServer, setSlotState, setSlotStatusDetail, setStopPressedAt, clearMessages, truncateAfterIndex, replaceMessages, hydrateSlotMessages, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage,
  sseContextUsage, setVoicePlaying, setVoiceAudio,
  toggleActivity, openActivityToTab, openActivityPanel, openActivityToTool, clearFocusToolCallId, clearSubagentsForSnapshot, sseSubagentPending, markSubagentApproving, sseSubagentSpawn, sseSubagentChunk, sseSubagentTool, sseSubagentStalled, sseSubagentRetrying, sseSubagentDone, sseSubagentQueued,
  sseSubagentBatchUpdate, sseSubagentBatchChunks, selectSubagent, clearTerminalSubagents,
  setGoalLoops, sseGoalLoop,
  sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent,
  sseMcpAppRender,
  sseWorkflowEvent, clearWorkflowRun,
  sseSideResult, sideClose, sideOptimisticAppend, sideOptimisticRollback,
} = chatSlice.actions
export default chatSlice.reducer
