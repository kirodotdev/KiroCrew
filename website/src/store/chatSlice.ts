import { createSlice, createAsyncThunk, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { addSlotOptimistic, updateSlot, removeSlotOptimistic, markSlotRead, fetchSlots, slotSurfaceKey, sseSlots } from './dashboardSlice'
import { resolveDefaultColor } from '../utils/sessionColors'
import { gcSessionStorage } from '../utils/storageGc'
import type { RootState } from './index'
import type { ChatMessage, ChatSlot, SessionInfo, SubagentActivity, ToolActivity } from '../types'
import { SOFT_STOP_DEBOUNCE_MS } from '../pages/chat/types'
import { mergePreservedPastes } from '../utils/pasteTokens'
import { safeSetItem } from '../utils/safeStorage'

const SKIP_ROLES = new Set(['chunk', 'done'])
const filterMessages = (msgs: ChatMessage[]) => msgs.filter(m => !SKIP_ROLES.has(m.role))

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
  slotContextTokens: Record<string, { used: number; window: number }>
  voicePlaying: boolean
  voiceAudio: string | null  // base64 stitched MP3 for replay
  subagents: Record<string, SubagentActivity>
  toolLog: ToolActivity[]
  /** Live dynamic-workflow runs keyed by run_id. Populated from
   *  `workflow_run_event` WS broadcasts; consumed by WorkflowProgressBar. */
  workflowRuns: Record<string, WorkflowRunProgress>
  activityOpen: boolean
  activityTab: 'changes' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'
  /** Tool call to highlight & auto-expand inline. Set by openActivityToTool;
   *  consumed (cleared) once the matching ToolCallLine has expanded itself. */
  focusToolCallId: string | null
  slotActivity: Record<string, { toolLog: ToolActivity[]; subagents: Record<string, SubagentActivity>; activityTab?: 'changes' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'; activityOpen?: boolean }>
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
  pendingQuestion: { slot: string; questions: Array<{ question: string; header?: string; options: Array<{ label: string; description?: string }>; multiSelect?: boolean }> } | null
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
  toolLog: [],
  workflowRuns: {},
  activityOpen: false,
  activityTab: 'files' as const,
  focusToolCallId: null,
  slotActivity: seedSlotActivity(),
  slotMessages: {},
  slotRun: {},
  slotHydrated: {},
  slotLoading: false,
  slotSide: {},
  slotSideClosed: {},
  slotHistory: [],
  pendingQuestion: null,
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
  const msgs = (state.slotMessages[slot] ??= [])
  const run = (state.slotRun[slot] ??= { state: 'idle' })
  const sa = (state.slotActivity[slot] ??= { toolLog: [], subagents: {} })
  const toolLog = sa.toolLog

  const effectiveKind = kind ?? (meta?.kind as string | undefined)
  if (effectiveKind === 'stop_event') {
    const id = (meta?.id as string) ?? ''
    const idx = id ? msgs.findIndex(m => m.meta?.id === id) : -1
    const msg: ChatMessage = { role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' }
    if (idx >= 0) msgs[idx] = msg
    else msgs.push(msg)
    return
  }
  if (role === '_segment') {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') {
        const raw = msgs[i].content
        const isPlaceholder = !raw || (/^[\s.\-…·•–—]{2,}$/.test(raw) && /[.\-…·•–—]/.test(raw)) || raw === '…'
        if (isPlaceholder) { msgs.splice(i, 1) } else { msgs[i].role = 'assistant'; msgs[i].rawText = msgs[i].content }
        break
      }
    }
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
      msgs.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content })
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
  if (role === 'tool') {
    run.state = 'tool_running'
    let insertIdx = msgs.length
    if (insertIdx > 0 && msgs[insertIdx - 1]?.role === 'streaming') insertIdx--
    msgs.splice(insertIdx, 0, { role, content, cls: cls || '', ts, meta })
    return
  }
  if (role === 'thinking') {
    if (!msgs.some(m => m.role === 'thinking')) msgs.push({ role: 'thinking', content: '', cls: '' })
    return
  }
  if (role === 'assistant') {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'streaming') { msgs[i].role = 'assistant'; msgs[i].content = content; if (ts) msgs[i].ts = ts; return }
    }
  }
  if (role === 'user') {
    sa.toolLog = []
    for (const m of msgs) {
      if (m.role === 'permission' && !m.meta?.resolved) { if (m.meta) m.meta.resolved = 'rejected'; else m.meta = { resolved: 'rejected' } }
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
  let effectiveMeta = meta
  if (role === 'permission' && !meta?.approval_id && cls) {
    try {
      const parsed = JSON.parse(cls)
      if (parsed.request_id) {
        effectiveMeta = { ...meta, approval_id: parsed.request_id, tool_input: parsed.tool_input ?? '', is_read_only: parsed.is_read_only ?? '', ...(parsed.tool_call_id ? { tool_call_id: parsed.tool_call_id } : {}), ...(parsed.resolved ? { resolved: parsed.resolved } : {}) }
      }
    } catch { /* not JSON cls, ignore */ }
  }
  msgs.push({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind })
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
/** Per-slot pending tool-approval (unresolved permission after the slot's last
 *  user message) — slot-aware version of ChatInput's old selectPendingApproval,
 *  so each grid pane's approval bar reflects ITS slot, not the global active one. */
export const selectSlotPendingApproval = (state: RootState, slot: string | null): ChatMessage | null => {
  const msgs = slot ? selectSlotMessages(state, slot) : state.chat.messages
  let lastUserIdx = -1
  for (let i = msgs.length - 1; i >= 0; i--) { if (msgs[i].role === 'user') { lastUserIdx = i; break } }
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
  return { key, messages: filterMessages(d.messages || []), running: d.running || false, stopping: d.stopping || false, hasMore: d.has_more || false, total: d.total || 0, queue: ((d.queue || []) as QueueItem[]).map((q: QueueItem) => typeof q === 'string' ? { content: q, queueId: crypto.randomUUID(), ts: new Date().toISOString() } : { content: q.content, queueId: q.id, ts: new Date().toISOString() }) }
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
  { agent?: string; model?: string; mode?: string; memory_mode?: string; clean_mode?: boolean; folder_id?: string | null; color_index?: number | null; project?: string | null } | string | undefined,
  { fulfilledMeta: { originActiveSlot: string | null } }
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
    // Capture the active slot BEFORE the (potentially slow) create round-trip.
    // The fulfilled reducer compares this against the active slot at resolution
    // time: if the user switched to a different session while the create was
    // pending (e.g. New Chat spun on "Creating" under memory pressure and they
    // moved to another tab), the new slot must NOT hijack the view. Mesh-2908.
    const originActiveSlot = (getState() as RootState).chat.activeSlot
    const slot = await api.createChatSlot(undefined, agent, model, mode, memory_mode, undefined, clean_mode)
    const dashState = (getState() as RootState).dashboard
    // An explicit color (e.g. carried from a slot being recreated on a
    // mode switch) wins; otherwise fall back to the default-color policy.
    const ci = explicitColor != null ? explicitColor : resolveDefaultColor(dashState.sessionDefaultColor, dashState.slots.length)
    if (ci != null) {
      slot.color_index = ci
      api.setSlotColor(slot.key, ci).catch(() => {})
    }
    // Carry folder membership so a recreated slot stays in its folder
    // instead of popping out to the top level.
    if (folderId) {
      slot.folder_id = folderId
      api.setSlotFolder(slot.key, folderId).catch(() => {})
    }
    // Carry the project directory. The create endpoint ignores `project` and
    // defaults it to the workspace dir, so a recreated slot would otherwise
    // lose its project — re-apply it via the dedicated endpoint. (We do NOT
    // re-issue setSlotAgent here: that endpoint resets the project back to the
    // workspace default, which would clobber this carry. Agent rides the
    // create payload instead.)
    if (project) {
      slot.project = project
      api.chatSlotProject(slot.key, project).catch(() => {})
    }
    dispatch(addSlotOptimistic(slot))
    // Carry the origin slot in the action meta (fulfillWithValue) rather than on
    // the payload, so it can never leak into the persisted slot object. The
    // fulfilled reducer reads action.meta.originActiveSlot to decide whether
    // activating the new slot is safe.
    return fulfillWithValue(slot, { originActiveSlot })
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
  return !!state.dashboard.slots.find((sl) => sl.key === slot)?.subagents_running
}

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    setActiveSlot(state, action: PayloadAction<string | null>) { state.activeSlot = action.payload; state.slotState = 'idle'; state.pendingTurnSlot = null },
    clearSlotState(state) { state.messages = []; state.toolLog = []; state.subagents = {}; state.activityTab = 'files'; state.slotRunning = false; state.slotStopping = false; state.slotState = 'idle'; state.slotHasMore = false; state.slotOldestIndex = 0; state.loadingOlder = false; state.lastChunkSeq = undefined; state._wsChunkedDuringFetch = false; state.slotStatusDetail = {}; state.voicePlaying = false; state.voiceAudio = null; state.pendingQuestion = null; state.pendingTurnSlot = null },
    setPendingInput(state, action: PayloadAction<string | null>) { state.pendingInput = action.payload },
    setQuestionCard(state, action: PayloadAction<ChatState['pendingQuestion']>) { state.pendingQuestion = action.payload },
    clearQuestionCard(state) { state.pendingQuestion = null },
    sseContextUsage(state, action: PayloadAction<{ slot: string; pct: number; used_tokens?: number; window_tokens?: number }>) {
      const { slot, pct, used_tokens, window_tokens } = action.payload
      state.slotContextPct[slot] = pct
      if (window_tokens && window_tokens > 0) {
        state.slotContextTokens[slot] = { used: used_tokens ?? 0, window: window_tokens }
      }
    },
    appendMessage(state, action: PayloadAction<ChatMessage>) { state.messages.push(action.payload) },
    /** Optimistically append a message to a specific slot's store — global
     *  `messages` when it's the active slot, else `slotMessages[slot]`. Lets a
     *  grid pane show a just-sent user message immediately in the right place. */
    appendSlotMessage(state, action: PayloadAction<{ slot: string; message: ChatMessage }>) {
      const { slot, message } = action.payload
      const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[slot] ??= [])
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
          if (message.ts) bubble.ts = message.ts
          bubble.meta = { ...(bubble.meta || {}), ...(message.meta || {}) }
          delete (bubble.meta as Record<string, unknown>).optimistic
          return
        }
      }
      msgs.push(message)
    },
    updateStreamingMessage(state, action: PayloadAction<string>) {
      const last = state.messages[state.messages.length - 1]
      if (last?.role === 'streaming') { last.content = action.payload }
      else { state.messages.push({ role: 'streaming', content: action.payload, cls: 'msg msg-a' }) }
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
    setStopPressedAt(state, action: PayloadAction<{ slotId: string; ts: number }>) { state.stopPressedAt[action.payload.slotId] = action.payload.ts },
    setSlotState(state, action: PayloadAction<SlotState>) { state.slotState = action.payload },
    setSlotStatusDetail(state, action: PayloadAction<{ slot: string; kind: string; text: string; ts: number; toolName?: string }>) {
      const { slot, ...detail } = action.payload
      state.slotStatusDetail[slot] = detail
    },
    clearMessages(state) { state.messages = []; state.slotHasMore = false; state.slotOldestIndex = 0; state.voiceAudio = null; state.voicePlaying = false },
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
      if (slot === state.activeSlot) return
      if (state.slotHydrated?.[slot]) return
      const cur = state.slotMessages[slot] ?? []
      state.slotMessages[slot] = [...messages, ...cur]
      if (!state.slotHydrated) state.slotHydrated = {}
      state.slotHydrated[slot] = true
    },
    setVoicePlaying(state, action: PayloadAction<boolean>) { state.voicePlaying = action.payload },
    setVoiceAudio(state, action: PayloadAction<string | null>) { state.voiceAudio = action.payload },
    toggleActivity(state) { state.activityOpen = !state.activityOpen; if (!state.activityOpen) state.focusToolCallId = null; persistActivityOpen(state.activeSlot, state.activityOpen) },
    openActivityPanel(state) { state.activityOpen = true; persistActivityOpen(state.activeSlot, true) },
    openActivityToTab(state, action: PayloadAction<'changes' | 'subagents' | 'workflows' | 'logs' | 'files' | 'side' | 'artifacts'>) { state.activityOpen = true; state.activityTab = action.payload; state.focusToolCallId = null; persistActivityOpen(state.activeSlot, true) },
    /** Tools tab is deprecated — tool details now expand inline in the chat. This action
     *  signals the matching ToolCallLine pill to auto-expand and scroll into view. */
    openActivityToTool(state, action: PayloadAction<string>) { state.focusToolCallId = action.payload },
    /** Clear after the matching pill has consumed the focus signal, so the same trigger
     *  doesn't re-fire on subsequent re-renders. */
    clearFocusToolCallId(state) { state.focusToolCallId = null },
    sseSubagentPending(state, action: PayloadAction<{ slot: string; id: string; task: string; approval_id: string }>) {
      const entry: SubagentActivity = {
        id: action.payload.id, task: action.payload.task, agent: '',
        status: 'pending', streaming: '', lastTool: '', startedAt: Date.now(), elapsed: 0,
        approval_id: action.payload.approval_id,
      }
      if (action.payload.slot !== state.activeSlot) {
        const c = state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }
        c.subagents[action.payload.id] = entry
        return
      }
      state.subagents[action.payload.id] = entry
    },
    markSubagentApproving(state, action: PayloadAction<{ id: string; approving: boolean }>) {
      const a = state.subagents[action.payload.id]
      if (a) { a.approving = action.payload.approving; return }
      for (const sa of Object.values(state.slotActivity)) {
        const b = sa.subagents[action.payload.id]
        if (b) { b.approving = action.payload.approving; return }
      }
    },
    sseSubagentSpawn(state, action: PayloadAction<{ slot: string; id: string; task: string; agent: string }>) {
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).subagents
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
      subs[action.payload.id] = {
        id: action.payload.id, task: action.payload.task, agent: action.payload.agent || 'kirocrew',
        status: 'running', streaming: existing?.streaming || '', lastTool: '', startedAt: existing?.startedAt || Date.now(), elapsed: 0,
      }
    },
    sseSubagentChunk(state, action: PayloadAction<{ slot: string; id: string; text: string }>) {
      const a = getSlotSubs(state, action.payload.slot)?.[action.payload.id]
      if (a) {
        a.streaming += action.payload.text
        if (a.streaming.length > 50_000) {
          a.streaming = '…(truncated)\n' + a.streaming.slice(-40_000)
        }
      }
    },
    sseSubagentTool(state, action: PayloadAction<{ slot: string; id: string; tool: string }>) {
      const a = getSlotSubs(state, action.payload.slot)?.[action.payload.id]
      if (a) { a.lastTool = action.payload.tool; a.status = 'tool' }
    },
    sseSubagentDone(state, action: PayloadAction<{ slot: string; id: string; elapsed: number; error?: string; task?: string; agent?: string; result?: string }>) {
      const subs = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      let a = subs[action.payload.id]
      if (!a) {
        // Cross-slot fallback: the card may live under a different slot key
        // than the done event's slot (e.g. the parent session was reset, or
        // the pending card was created under the activeSlot fallback). Find
        // it by id anywhere so the card doesn't stay stuck "running" forever.
        if (state.subagents[action.payload.id]) a = state.subagents[action.payload.id]
        else {
          for (const sa of Object.values(state.slotActivity)) {
            if (sa.subagents[action.payload.id]) { a = sa.subagents[action.payload.id]; break }
          }
        }
      }
      if (a) {
        a.status = action.payload.error ? 'error' : 'done'
        a.elapsed = action.payload.elapsed
        a.error = action.payload.error
        a.streaming = ''
        if (action.payload.task && !a.task) a.task = action.payload.task
      }
      else { subs[action.payload.id] = { id: action.payload.id, task: action.payload.task || '', agent: action.payload.agent || 'kirocrew', status: action.payload.error ? 'error' : 'done', streaming: '', lastTool: '', startedAt: Date.now() - action.payload.elapsed * 1000, elapsed: action.payload.elapsed, error: action.payload.error } }
    },
    sseSideResult(state, action: PayloadAction<{ slot: string; run_id: string; role: 'user' | 'assistant'; content: string; ts?: number; is_error?: boolean; final?: boolean }>) {
      const { slot, run_id, role, content, ts, is_error, final } = action.payload
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
        state.slotSide[slot] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: tsIso }
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
      state.slotSideClosed[action.payload] = true
    },
    sideOptimisticAppend(state, action: PayloadAction<{ slot: string; message: SideMessage }>) {
      const { slot, message } = action.payload
      if (state.slotSideClosed[slot]) delete state.slotSideClosed[slot]
      if (!state.slotSide[slot]) {
        const parentTurnCount = slot === state.activeSlot
          ? state.messages.filter(m => m.role === 'user' || m.role === 'assistant').length
          : 0
        state.slotSide[slot] = { messages: [], openedAtTurnCount: parentTurnCount, createdAt: message.ts }
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
    sseSubagentSnapshot(state, action: PayloadAction<{ id: string; slot: string; task: string; agent: string; streaming: string; last_tool: string; started: number }>) {
      const d = action.payload
      const subs = d.slot && d.slot !== state.activeSlot
        ? (state.slotActivity[d.slot] ??= { toolLog: [], subagents: {} }).subagents
        : state.subagents
      const existing = subs[d.id]
      subs[d.id] = {
        id: d.id, task: d.task, agent: d.agent || 'kirocrew',
        status: d.last_tool ? 'tool' : 'running', streaming: d.streaming, lastTool: d.last_tool,
        startedAt: d.started * 1000, elapsed: 0,
        approval_id: existing?.approval_id, approving: existing?.approving,
      }
    },
    /** Fold a single dynamic-workflow run event into workflowRuns. */
    sseWorkflowEvent(state, action: PayloadAction<{ run_id: string; session_key?: string; seq?: number; ts?: number; type: string; data?: Record<string, unknown> }>) {
      const { run_id, type, data, session_key } = action.payload
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
      state.workflowRuns[run_id] = cur
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
    sseToolActivity(state, action: PayloadAction<{ slot: string; tool: string; kind: string; purpose: string; input_preview: string; auto?: boolean; tool_call_id?: string; is_update?: boolean }>) {
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).toolLog
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
          existing.ts = Date.now()
          return
        }
      }
      log.push({ type: 'tool', text: action.payload.tool, purpose: action.payload.purpose, input: action.payload.input_preview, ts: Date.now(), auto: action.payload.auto, tool_call_id: action.payload.tool_call_id })
      if (log.length > 100) log.splice(0, log.length - 100)
    },
    sseActivityEvent(state, action: PayloadAction<{ slot: string; kind: string; text: string; approval_id?: string; approval_type?: string }>) {
      const log = action.payload.slot !== state.activeSlot
        ? (state.slotActivity[action.payload.slot] ??= { toolLog: [], subagents: {} }).toolLog
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
      const log = action.payload.slot !== state.activeSlot
        ? state.slotActivity[action.payload.slot]?.toolLog
        : state.toolLog
      if (!log) return
      const tid = action.payload.tool_call_id
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].type === 'tool' && (!tid || log[i].tool_call_id === tid || !log[i].tool_call_id)) {
          log[i].output = action.payload.output; break
        }
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
      state.messages.push({ role: 'thinking', content, cls: '' })
    },
    sseChatMessage(state, action: PayloadAction<{ slot: string; role: string; content: string; ts?: string; seq?: number; cls?: string; meta?: Record<string, unknown>; kind?: string; batched?: boolean }>) {
      const { slot, role, content, ts, seq, cls, meta, kind, batched } = action.payload
      if (slot !== state.activeSlot) { applyNonActiveFrame(state, action.payload); return }
      // stop_event — replace in place by id, or insert new
      const effectiveKind = kind ?? (meta?.kind as string | undefined)
      if (effectiveKind === 'stop_event') {
        const id = (meta?.id as string) ?? ''
        const idx = id ? state.messages.findIndex(m => m.meta?.id === id) : -1
        const msg: ChatMessage = { role, content, cls: cls || '', ts, meta: { ...meta, kind: 'stop_event' }, kind: 'stop_event' }
        if (idx >= 0) { state.messages[idx] = msg } else { state.messages.push(msg) }
        return
      }
      // WS segment — finalize streaming into assistant without resetting sequence or slot state
      if (role === '_segment') {
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            // Drop trivially meaningless placeholder content that the model
            // emits before tool calls ("...", "…", "---", ". . .", etc.).
            // Only drop patterns that are EXCLUSIVELY composed of 2+ repeated
            // punctuation/whitespace chars — never single characters which could
            // be the start of legitimate content (list markers, etc.).
            const raw = state.messages[i].content
            const isPlaceholder = !raw || (/^[\s.\-…·•–—]{2,}$/.test(raw) && /[.\-…·•–—]/.test(raw)) || raw === '…'
            if (isPlaceholder) {
              state.messages.splice(i, 1)
            } else {
              state.messages[i].role = 'assistant'
              state.messages[i].rawText = state.messages[i].content
            }
            break
          }
        }
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
          state.messages.push({ role: 'streaming', content, cls: 'msg msg-a', rawText: content })
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
      // Tool call — update state, insert before streaming message
      if (role === 'tool') {
        state.slotState = 'tool_running'
        // Insert tool before any trailing streaming message so
        // chat_segment can still find and finalize it with redacted text.
        let insertIdx = state.messages.length
        if (insertIdx > 0 && state.messages[insertIdx - 1]?.role === 'streaming') {
          insertIdx--
        }
        state.messages.splice(insertIdx, 0, { role, content, cls: cls || '', ts, meta })
        return
      }
      // Thinking — deduplicate, only keep one
      if (role === 'thinking') {
        if (state.messages.some(m => m.role === 'thinking')) return
        state.messages.push({ role: 'thinking', content: '', cls: '' })
        return
      }
      // Replace streaming placeholder with final assistant message
      if (role === 'assistant') {
        for (let i = state.messages.length - 1; i >= 0; i--) {
          if (state.messages[i].role === 'streaming') {
            state.messages[i].role = 'assistant'; state.messages[i].content = content; if (ts) state.messages[i].ts = ts
            return
          }
        }
      }
      // New user message = new turn — clear activity log
      if (role === 'user') {
        state.toolLog = []
        // Auto-resolve any stale permissions from previous turn so they don't block the new turn
        for (const m of state.messages) {
          if (m.role === 'permission' && !m.meta?.resolved) {
            if (m.meta) m.meta.resolved = 'rejected'
            else m.meta = { resolved: 'rejected' }
          }
        }
      }
      // Permission messages carry request_id/tool_input in cls (JSON) — lift into meta
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
      state.messages.push({ role, content, cls: cls || '', ts, meta: effectiveMeta, kind })
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
      const msgs = slot === state.activeSlot ? state.messages : state.slotMessages[slot]
      if (!msgs) return
      const idx = msgs.findIndex(m => m.role === 'queued' && (m.meta?.queueId as string) === queue_id)
      if (idx >= 0) msgs[idx].content = content
    },
    /** Add a queued message (from backend queue_push WS event). */
    appendQueuedMessage: {
      reducer(state, action: PayloadAction<{ slot: string; content: string; ts: string; queueId: string }>) {
        const { slot, content, ts, queueId } = action.payload
        const msgs = slot === state.activeSlot ? state.messages : (state.slotMessages[slot] ??= [])
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
       *  Sessions that close/archive/delete vanish from the SSE `slots` REPLACE,
       *  but their transcripts previously stayed resident for the tab's lifetime
       *  (only `deleteSlot.fulfilled` evicted) — the dominant retention class
       *  behind multi-GB heaps on long-lived dashboard tabs (Mesh-2835).
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
        // 'tools' tab was removed in May 2026 (inline expansion replaces it). Cached
        // pre-migration values fall back to 'files'.
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
          && !preserved.some(m => m.role === 'assistant' && m.content === lastLocal.content)
        ) {
          // The HTTP fetch resolved with a history that predates the reply we
          // already finalized locally (via applyNonActiveFrame while this slot
          // was backgrounded). Blindly replacing with the server response here
          // is the "switch away and back drops the latest response" regression.
          // Keep the server history but re-attach the local trailing reply.
          // Guarded by the content check above so we never duplicate a reply
          // the server already returned.
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
        // Hydrate queued messages from backend queue field
        // Clear any WS-delivered queued messages first to avoid duplicates
        // (a queue_push WS event may have arrived during the HTTP fetch)
        state.messages = state.messages.filter(m => m.role !== 'queued')
        for (const { content, queueId, ts } of queue) {
          state.messages.push({ role: 'queued', content, cls: 'msg msg-queued', ts, meta: { queueId } })
        }
        // Update cache and clear loading state
        state.slotMessages[key] = state.messages
        state.slotLoading = false
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
        const { key, messages, running, hasMore, total } = action.payload
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
        state.messages = mergePreservedThinking(state.messages, sorted)
        state.slotRunning = running
        state.slotStopping = action.payload.stopping ?? false
        state.pendingTurnSlot = null
        state.slotHasMore = hasMore
        state.slotOldestIndex = hasMore ? total - messages.length : 0
      })
      .addCase(warmSlotCache.fulfilled, (state, action) => {
        if (!action.payload) return
        const { key, messages } = action.payload
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
        state.slotMessages[key] = messages.map(m => {
          const aid = m.role === 'permission' ? (m.meta?.approval_id as string | undefined) : undefined
          return aid && localResolved.has(aid)
            ? { ...m, meta: { ...m.meta, resolved: localResolved.get(aid) } }
            : m
        })
        // Clear the per-slot run indicator (the _done frame already idles it;
        // this is belt-and-braces for the fetch-completes-after-_done ordering).
        const run = (state.slotRun[key] ??= { state: 'idle' })
        run.state = 'idle'
        run.lastChunkSeq = undefined
      })
      .addCase(createSlot.pending, (state) => { state.creatingSlot = true })
      .addCase(createSlot.rejected, (state) => { state.creatingSlot = false })
      .addCase(createSlot.fulfilled, (state, action) => {
        // The create POST resolved, so clear the pending flag regardless of
        // whether we activate below. Otherwise the switched-away early-return
        // would strand the "Creating…" spinner on forever.
        state.creatingSlot = false
        // Switched-away guard (Mesh-2908): if the user moved to a different
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
          // 'tools' tab was removed (inline expansion replaces it). Cached pre-migration values fall back to 'files'.
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
  setActiveSlot, clearSlotState, setPendingInput, setQuestionCard, clearQuestionCard, appendMessage, appendSlotMessage, updateStreamingMessage, finalizeAssistant,
  removeThinking, removeByApprovalId, resolveByApprovalId, clearPendingPermissions, setSlotRunning, setSlotStopping, startLocalTurn, syncSlotRunningFromServer, setSlotState, setSlotStatusDetail, setStopPressedAt, clearMessages, truncateAfterIndex, replaceMessages, hydrateSlotMessages, sseChatMessage, sseChatMessageUpdate, sseChatMessagePatchByTs, sseThinkingChunk, removeQueuedMessage, appendQueuedMessage, cancelQueuedMessage, editQueuedMessage,
  sseContextUsage, setVoicePlaying, setVoiceAudio,
  toggleActivity, openActivityToTab, openActivityPanel, openActivityToTool, clearFocusToolCallId, sseSubagentPending, markSubagentApproving, sseSubagentSpawn, sseSubagentChunk, sseSubagentTool, sseSubagentDone,
  sseSubagentSnapshot, sseToolActivity, sseToolResult, sseActivityEvent,
  sseWorkflowEvent, clearWorkflowRun,
  sseSideResult, sideClose, sideOptimisticAppend, sideOptimisticRollback,
} = chatSlice.actions
export default chatSlice.reducer
