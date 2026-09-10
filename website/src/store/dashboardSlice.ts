import { safeSetItem } from '../utils/safeStorage'
import { newerTs } from '../lib/slotReadRelay'
import { jsonEqual } from '../utils/structuralEqual'
import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { sanitizeLlmOutput, isUnsafeKey } from '../utils/sanitize'
import type { StatusData, ChatSlot, TodoList, McpSessionReport } from '../types'
import type { SessionColorMode, PaletteName, DefaultColorSetting, IntensityName } from '../utils/sessionColors'

export interface SubagentDetail {
  id: string; task: string; agent: string; turns: number; last_tool: string; startedAt: number
}

interface DashboardState {
  status: StatusData | null
  /** The ad-hoc auto-approve duration this tab last saved in Settings, or
   *  undefined when it has saved none. Applied over every status write: the
   *  save is the newest fact this tab holds, and a status reply that began
   *  before it (the boot read, a slow earlier request) can carry the older
   *  value. Reset by a page load, whose boot read then reads the stored one. */
  savedYoloDuration?: NonNullable<StatusData['yolo_duration']>
  connected: boolean
  slots: ChatSlot[]
  /** Increments for every accepted authoritative full-slot frame/reply. */
  slotsGeneration: number
  /** Per-key optimistic/reconciliation pin writes, independent of other slot fields. */
  slotPinGenerations: Record<string, number>
  // Slot keys in the order the session sidebar actually DISPLAYS them
  // (pinned-first + the user's sort, flat-view aware). Published by
  // ChatSidebar; consumed by the chat-jump / chat-cycle keyboard shortcuts so
  // Ctrl/Alt+N targets the Nth visible row rather than the Nth element of
  // `slots` (which arrives in backend insertion order). Empty until the
  // sidebar first renders — consumers fall back to `slots` order then.
  sidebarOrder: string[]
  approvalMode: string
  channelTrusted: boolean
  refreshTrigger: number
  unreadSlots: string[]
  /** Watermarks for relayed clears: slot -> newest message ts that marked it
   *  unread. A cross-window `slot_read` clears the badge only when its read
   *  watermark chronologically covers this value, so an in-flight relay
   *  cannot erase a badge a NEWER message lit. A manual mark-as-unread
   *  records the MANUAL_UNREAD sentinel, which no watermark covers — the
   *  deliberate note to self answers only to this window. Message watermarks
   *  are persisted in the SHARED store next to the badges they protect: any
   *  window that boots — a reload OR a brand-new tab — restores each badge
   *  with its watermark, so a stale relay can never clear a badge lit by a
   *  message the reader had not seen, in any window. Sentinels persist
   *  per-tab and never publish shared state. Badge and watermark persist
   *  as ONE shared record entry written atomically, so the pair can never
   *  tear apart and there are no orphans to reconcile at boot. */
  unreadSince: Record<string, string>
  slotsLoaded: boolean
  updateProgress: { step: string; detail: string } | null
  // Desktop updater: an update is discoverable/staged (found|downloading|
  // downloaded). Drives the Settings nav dot + the About tab dot. Mirrored
  // from the Electron update-state events by useUpdateSubscription.
  desktopUpdateAvailable: boolean
  subagentRunning: Record<string, number>
  subagentDetails: Record<string, SubagentDetail[]>
  subagentText: Record<string, Record<string, string>>
  sessionDefaultColor: DefaultColorSetting
  sessionColorsMode: SessionColorMode
  sessionColorsPalette: PaletteName
  sessionColorsIntensity: IntensityName
  enabledAppIds: string[]
}

const safeGet = (key: string, fallback: string) => { try { return localStorage.getItem(key) ?? fallback } catch { return fallback } }
/** unreadSince sentinel for a manual mark-as-unread. It parses as an invalid
 *  instant, so the conservative comparison below can never treat any relayed
 *  read watermark as covering it. A bare non-letter char, never rendered —
 *  the constant name carries the meaning. */
export const MANUAL_UNREAD = '\uffff'

/** True when `read` chronologically covers `since`. Timestamps are parsed as
 *  instants — mixed-offset server strings make lexical order lie about time
 *  order — and ANY unparseable side answers false, so an invalid watermark
 *  can never clear a badge (and the manual sentinel never parses). */
const readCovers = (read: string | undefined, since: string): boolean => {
  if (read === undefined) return false
  const r = Date.parse(read)
  const s = Date.parse(since)
  return Number.isFinite(r) && Number.isFinite(s) && r >= s
}


/** THE shared unread record ('mc-unread-shared' in localStorage): slot ->
 *  message watermark, or '' for a badge no watermark guards (any relayed
 *  read clears it). Badge presence and watermark are one key in one JSON
 *  document written by ONE setItem, so a sibling tab can never observe a
 *  badge without its watermark or a watermark without its badge — there is
 *  no torn state to reconcile at boot. That guarantee is single-WRITE
 *  atomicity only: localStorage has no cross-process transaction, so two
 *  windows' simultaneous RMWs race last-writer-wins on the whole document.
 *  Per-slot deltas keep any lost update slot-local, and it self-heals on
 *  that slot's next arrival or relay. Writes are per-slot DELTAS with
 *  newest-parseable-ts-wins: two windows writing the same slot settle on
 *  the newest instant, keys this window never touched pass through, and a
 *  ''-arrival never demotes a real watermark. MANUAL_UNREAD sentinels are
 *  deliberately NOT here: the reminder answers only to its own window, so
 *  sentinels persist to per-tab sessionStorage via persistManualSentinels.
 *  'mc-unread-slots' is kept as a write-only PROJECTION of the record's
 *  keys — the pre-existing hub relay (safeSet) and tabs still running
 *  older code read it; nothing in this file does. */
const persistSharedUnread = (add: Record<string, string>, remove: readonly string[]): void => {
  try {
    let stored: Record<string, string>
    try { stored = JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string> } catch { stored = {} }
    for (const k of remove) delete stored[k]
    for (const [k, v] of Object.entries(add)) {
      if (v === MANUAL_UNREAD) continue  // sentinels never publish
      const prev = stored[k]
      if (prev === undefined) { stored[k] = v; continue }
      if (v === '') continue  // presence already recorded; never demote a watermark
      stored[k] = prev === '' ? v : (newerTs(prev, v) ?? prev)
    }
    localStorage.setItem('mc-unread-shared', JSON.stringify(stored))
    // Projection write bypasses safeSet's hub relay: the shared keys omit
    // this window's manual sentinels, so relaying their count would under-
    // report the hub switcher chip. The reducers relay the window's own
    // unreadSlots count after every unread mutation instead.
    safeSetItem('mc-unread-slots', JSON.stringify(Object.keys(stored)))
  } catch { /* SecurityError / quota */ }
}
/** Clear one slot's SHARED unread record only when `readTs` covers the
 *  watermark CURRENTLY PERSISTED — the live stored value, read inside this
 *  call, never this window's in-memory view, which can be stale across a
 *  reconnect gap. A ''-record (badge, no watermark) accepts any read.
 *  Returns undefined when the record was cleared; returns the surviving
 *  watermark when a sibling advanced it past this read — the caller then
 *  keeps the badge and adopts that watermark instead of erasing a newer
 *  window's state. */
const clearSharedUnreadIfCovered = (slot: string, readTs: string | undefined): string | undefined => {
  let sharedW: string | undefined
  try {
    sharedW = (JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)[slot]
  } catch { sharedW = undefined }
  if (sharedW !== undefined && sharedW !== '' && !readCovers(readTs, sharedW)) return sharedW
  persistSharedUnread({}, [slot])
  return undefined
}
/** This window's manual reminders, per-tab (sessionStorage): a deliberate
 *  mark-as-unread answers only to the window that made it, so a shared key
 *  would clobber siblings' reminder sets. */
const persistManualSentinels = (unreadSince: Record<string, string>): void => {
  const manual = Object.fromEntries(Object.entries(unreadSince).filter(([, v]) => v === MANUAL_UNREAD))
  try { sessionStorage.setItem('mc-unread-since', JSON.stringify(manual)) } catch { /* SecurityError / quota */ }
}
/** Boot restore for unreadSince (exported for tests): message watermarks
 *  from the ONE shared record, joined with this window's per-tab manual
 *  sentinels. A ''-entry is a badge no watermark guards — it restores the
 *  badge (restoreUnreadBadges below) but records no watermark, so any
 *  relayed read clears it. An ABSENT record beside a legacy
 *  'mc-unread-slots' list means older code persisted badges before the
 *  record existed: each seeds once as '' so no badge is lost on upgrade.
 *  Sentinels: no other window's read may clear the deliberate reminder,
 *  and neither reload nor the shared record may demote it — the sentinel
 *  wins a key collision, and restoreUnreadBadges() re-seeds its badge
 *  without writing shared state. */
export const restoreUnreadSince = (): Record<string, string> => {
  try {
    const raw = localStorage.getItem('mc-unread-shared')
    let record: Record<string, string>
    try { record = JSON.parse(raw ?? '{}') as Record<string, string> } catch { record = {} }
    if (raw === null) {
      let legacy: string[]
      try { legacy = JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]') as string[] } catch { legacy = [] }
      for (const k of legacy) record[k] = ''
      if (legacy.length > 0) localStorage.setItem('mc-unread-shared', JSON.stringify(record))
    }
    const since: Record<string, string> = {}
    for (const [k, v] of Object.entries(record)) if (v !== '') since[k] = v
    let manual: Record<string, string>
    try { manual = JSON.parse(sessionStorage.getItem('mc-unread-since') ?? '{}') as Record<string, string> } catch { manual = {} }
    for (const [k, v] of Object.entries(manual)) if (v === MANUAL_UNREAD) since[k] = MANUAL_UNREAD
    return since
  } catch { return {} }
}
/** Boot restore for unreadSlots: every key of the shared record (badge
 *  presence IS record membership), plus this window's manual reminders —
 *  the reminder answers only to this window, so its badge comes back here
 *  without writing shared state. */
export const restoreUnreadBadges = (since: Record<string, string>): string[] => {
  let badges: string[]
  try { badges = Object.keys(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>) } catch { badges = [] }
  for (const [k, v] of Object.entries(since)) {
    if (v === MANUAL_UNREAD && !badges.includes(k)) badges.push(k)
  }
  return badges
}
// When running embedded inside the Instances hub (an iframe), relay unread-count
// changes to the parent so it can badge this instance's switcher chip (§5.3).
// Only the count (a non-secret number) is sent; the parent validates event.origin
// against its known tunnel origins before trusting it (§5.4). Posting to the
// referrer's origin (the hub) when known, else '*', avoids broadcasting widely.
const _relayUnreadToParent = (slotsJson: string): void => {
  try {
    if (typeof window === 'undefined' || window.parent === window) return
    const count = (JSON.parse(slotsJson) as string[]).length
    let target = '*'
    try { if (document.referrer) target = new URL(document.referrer).origin } catch { /* keep '*' */ }
    window.parent.postMessage({ source: 'kirocrew', type: 'mc-unread-slots', count }, target)
  } catch { /* never let the relay break a state update */ }
}
const safeSet = (key: string, value: string) => {
  try { safeSetItem(key, value) } catch { /* QuotaExceededError / SecurityError */ }
  if (key === 'mc-unread-slots') _relayUnreadToParent(value)
}

const initialState: DashboardState = {
  status: null,
  connected: false,
  slots: [],
  slotsGeneration: 0,
  slotPinGenerations: {},
  sidebarOrder: [],
  approvalMode: 'normal',
  channelTrusted: false,
  refreshTrigger: 0,
  ...(() => { const since = restoreUnreadSince(); return { unreadSlots: restoreUnreadBadges(since), unreadSince: since } })(),
  slotsLoaded: false,
  updateProgress: null,
  desktopUpdateAvailable: false,
  subagentRunning: {},
  subagentDetails: {},
  subagentText: {},
  sessionDefaultColor: (() => { try { return (JSON.parse(localStorage.getItem('mc-session-default-color') ?? 'null') as DefaultColorSetting) ?? null } catch { return null } })(),
  sessionColorsMode: safeGet('mc-session-colors-mode', 'tint') as SessionColorMode,
  sessionColorsPalette: safeGet('mc-session-colors-palette', 'horizon') as PaletteName,
  sessionColorsIntensity: safeGet('mc-session-colors-intensity', 'clear') as IntensityName,
  enabledAppIds: [],
}

export const fetchSlots = createAsyncThunk('dashboard/fetchSlots', () => api.chatSlots())

/** Switch the approval mode, carrying a policy refusal back to the caller.
 *
 *  The gateway answers 403 `mode_disabled_by_policy` when the `approval_modes`
 *  scope forbids the mode. A plain `throw` would reach the reducer as
 *  `action.error.message` only, dropping the machine-readable code with it, so
 *  the caller could not tell a policy refusal from a network failure — and the
 *  picker would have nothing to show but silence. `rejectWithValue` keeps the
 *  code, which is what makes the refusal reportable next to the control. */
export const changeApprovalMode = createAsyncThunk<
  string,
  { mode: string; slot?: string },
  { rejectValue: { code: string; message: string } }
>(
  'dashboard/changeApprovalMode',
  async ({ mode, slot }, { rejectWithValue }) => {
    try {
      await api.chatMode(mode, slot)
    } catch (e) {
      const body = e instanceof ApiError ? e.body : ''
      let code = ''
      try { code = JSON.parse(body || '{}')?.code ?? '' } catch { /* not JSON */ }
      return rejectWithValue({
        code,
        message: e instanceof Error ? e.message : String(e),
      })
    }
    return mode
  },
)

/** Drop one slot's live sub-agent state.
 *
 *  These three maps are keyed by the bare slot key and are otherwise cleared
 *  only wholesale on reconnect, so a departed slot's counters and rows would
 *  otherwise survive for the tab's lifetime.
 *
 *  Driven by the AUTHORITATIVE slot-list writers — `sseSlots` and
 *  `fetchSlots.fulfilled` — and deliberately NOT by `removeSlotOptimistic`: that
 *  reducer runs before the delete is confirmed, and `sseSubagentText` drops every
 *  frame for a slot with no `subagentRunning` entry, so evicting optimistically
 *  would leave a slot whose delete failed alive but permanently mute. */
/** Reconcile per-slot dashboard state against an authoritative slot list. Both
 *  authoritative writers (`sseSlots`, `fetchSlots.fulfilled`) drive teardown
 *  through here, so the two cannot drift apart the way the eviction lists this
 *  PR unified once did. `unreadSlots` is written back only when it actually
 *  shrank, since the live-frame writer runs on every slots frame. */
const reconcileSlots = (state: DashboardState, liveKeys: Set<string>, evictStale = true): void => {
  // `countUnreadByMode` deliberately keeps orphan unread keys contributing to
  // the badge, on the premise that a reconcile drains them shortly. Draining on
  // both writers is what keeps that premise true. Always run: a wrongly drained
  // badge self-heals on the next unread event, and the refetch is the documented
  // route by which a remotely deleted slot's badge is cleared.
  const unread = state.unreadSlots ?? []
  const drained = unread.filter(k => liveKeys.has(k))
  if (drained.length !== unread.length) {
    // unreadSince tolerates partial preloaded test state, like `?? []` above.
    if (state.unreadSince) {
      let droppedManual = false
      for (const k of unread) if (!liveKeys.has(k)) {
        if (state.unreadSince[k] === MANUAL_UNREAD) droppedManual = true
        delete state.unreadSince[k]
      }
      if (droppedManual) persistManualSentinels(state.unreadSince)
    }
    state.unreadSlots = drained
    persistSharedUnread({}, unread.filter(k => !liveKeys.has(k)))
    _relayUnreadToParent(JSON.stringify(state.unreadSlots))
  }
  // Eviction is NOT recoverable, so it is skipped when the caller cannot vouch
  // for the list's freshness: an HTTP reply in flight can be older than the live
  // frames that arrived while it travelled, and would then delete a slot the
  // stream has since created.
  if (!evictStale) return
  for (const key of Object.keys(state.subagentRunning ?? {})) {
    if (!liveKeys.has(key)) evictSlotSubagents(state, key)
  }
}

const evictSlotSubagents = (state: DashboardState, slotKey: string): void => {
  delete state.subagentRunning[slotKey]
  delete state.subagentDetails[slotKey]
  delete state.subagentText[slotKey]
}

/** Apply an authoritative slot list, reusing the object identity of every row
 *  whose content is unchanged, and touching `state.slots` only when the list
 *  actually moved.
 *
 *  Membership AND order come from `next` — the server is authoritative on both.
 *  Only per-row identity is carried across, and only for a structurally equal
 *  row, so no consumer can read stale content off a reused reference. The
 *  comparison uses the shared `jsonEqual`, whose key-order independence and
 *  field-agnosticism this relies on: a row may have been patched in place by
 *  `touchSlotActivity` / `updateSlot` / `patchSlotLink` since it was stored (so
 *  its key order can differ from the payload's), and a comparator that listed
 *  `ChatSlot`'s fields would stop seeing a newly added one and pin a stale row
 *  on screen — a correctness bug, where an extra re-render is only a cost.
 *
 *  Identity is load-bearing here rather than a micro-optimisation. The sidebar
 *  renders every row as a Framer `motion.div` with `layout="position"` inside one
 *  `LayoutGroup`, and every selector over `dashboard.slots` invalidates when the
 *  array or any row changes reference. Assigning the incoming array wholesale
 *  hands every row a new reference on every frame, so one slot's status change
 *  re-renders and re-measures the entire list — which reads as the sidebar
 *  reloading rather than as one session becoming active. Slot pushes coalesce at
 *  200ms server-side, so a single active turn delivers several full lists per
 *  second and the effect is continuous.
 *
 *  Skipping the assignment (rather than assigning an equal array) is the half
 *  that matters most: it leaves the array reference alone, which lets a
 *  downstream `useMemo` skip its filter and sort entirely instead of recomputing
 *  an equal result. */
const applySlots = (state: DashboardState, next: ChatSlot[]): void => {
  const prev = state.slots ?? []
  const byKey = new Map(prev.map(s => [s.key, s]))
  let changed = prev.length !== next.length
  const merged = next.map((incoming, i) => {
    const existing = byKey.get(incoming.key)
    // Reusing a draft row inside a freshly assigned array is fine: Immer
    // finalizes drafts found in the assigned value within the same scope, so an
    // untouched row resolves back to its base object and keeps its identity.
    const reused = existing !== undefined && jsonEqual(existing, incoming) ? existing : incoming
    // Positional compare, so a pure reorder counts as changed even though every
    // row is individually reusable.
    if (reused !== prev[i]) changed = true
    return reused
  })
  if (changed) state.slots = merged
}

const dashboardSlice = createSlice({
  name: 'dashboard',
  initialState,
  reducers: {
    // Two writers feed this reducer with different field sets. The HTTP
    // `/api/status` reply carries the configured ad-hoc duration and whether
    // policy permits `until_shutdown`; the 5-second WebSocket `dashboard` frame
    // is built from the gateway's shared snapshot and omits both, because
    // resolving them costs a config read and a governance evaluation the push
    // loop must not pay. A frame is otherwise authoritative and REPLACES the
    // status (a key it omits is an answer -- e.g. an older gateway sending no
    // `version_display`), so only these two config-derived keys are carried
    // forward when a frame lacks them. Without that the first push drops them
    // and the approval-mode confirm card names the default 6-hour duration
    // whatever the operator configured. A duration this tab saved in Settings
    // outranks both the carried value and the payload's own: a reply that
    // began before the save can carry the older token. The live-grant fields
    // (`yolo_expires_at`, `yolo_until_shutdown`) are deliberately NOT carried:
    // they change on every activation, and a stale expiry is worse than none.
    sseStatus(state, action: PayloadAction<StatusData>) {
      const prev = state.status
      const next: StatusData = { ...action.payload }
      const duration = state.savedYoloDuration ?? next.yolo_duration ?? prev?.yolo_duration
      if (duration !== undefined) next.yolo_duration = duration
      if (next.yolo_until_shutdown_permitted === undefined && prev?.yolo_until_shutdown_permitted !== undefined) {
        next.yolo_until_shutdown_permitted = prev.yolo_until_shutdown_permitted
      }
      state.status = next
      state.connected = true
      // Sync YOLO from backend (authoritative source)
      if (action.payload.yolo !== undefined) {
        state.approvalMode = action.payload.yolo ? 'yolo' : (state.approvalMode === 'yolo' ? 'normal' : state.approvalMode)
      }
      // Sync update progress from status (for new tabs — pill indicator, not modal)
      if (action.payload.update_progress !== undefined) {
        state.updateProgress = action.payload.update_progress
      }
    },
    // A slots frame carries only the live YOLO boolean, not a status snapshot.
    // Keep the last authoritative status intact so fields such as yolo_duration
    // remain available to the approval-mode confirmation copy.
    sseYolo(state, action: PayloadAction<boolean>) {
      if (state.status) state.status.yolo = action.payload
      state.approvalMode = action.payload ? 'yolo' : (state.approvalMode === 'yolo' ? 'normal' : state.approvalMode)
    },
    // A duration the user just saved in Settings. The gateway stores the token
    // as sent, so no re-read is needed: the picker can name it at once, and
    // `sseStatus` keeps it over every later frame or reply, including one that
    // was already in flight when the save landed. Recorded even before the
    // first status arrives, so a save during cold load is not lost.
    setYoloDuration(state, action: PayloadAction<NonNullable<StatusData['yolo_duration']>>) {
      state.savedYoloDuration = action.payload
      if (state.status) state.status.yolo_duration = action.payload
    },
    sseConnected(state) { state.connected = true; state.slotsLoaded = false; state.subagentRunning = {}; state.subagentDetails = {}; state.subagentText = {} },
    sseDisconnected(state) { state.connected = false },
    sseSlots(state, action: PayloadAction<ChatSlot[]>) {
      // Read before `slotsLoaded` is set: an empty frame is ambiguous, and this
      // is what disambiguates it. Not yet loaded means a reconnect delivered it
      // before the first real snapshot, so treating it as authoritative would
      // evict every live slot's state. Already loaded means the list genuinely
      // went empty — the last slot was deleted, possibly by another client —
      // and skipping teardown there would strand its state permanently.
      // Return BEFORE writing anything: assigning an empty `slots` would blank
      // the sidebar until restoration finishes, and marking it loaded would
      // claim a snapshot arrived when none has.
      if (action.payload.length === 0 && !state.slotsLoaded) return
      applySlots(state, action.payload)
      state.slotsGeneration = (state.slotsGeneration ?? 0) + 1
      state.slotsLoaded = true
      reconcileSlots(state, new Set(action.payload.map(s => s.key)))
    },
    // Sidebar → shortcuts order feed (see DashboardState.sidebarOrder). The
    // dispatch site diff-guards, so every action here is a real order change.
    setSidebarOrder(state, action: PayloadAction<string[]>) { state.sidebarOrder = action.payload },
    // Live TODO-list delta. Patched into the SAME slots array that sseSlots
    // populates rather than a parallel map, so the mid-turn push and the
    // reconnect snapshot can never disagree about a slot's list. A delta for an
    // unknown slot is dropped — the next sseSlots push carries it anyway.
    sseTodoUpdate(state, action: PayloadAction<{ slot: string; todo: TodoList | null }>) {
      const slot = (state.slots ?? []).find(s => s.key === action.payload.slot)
      if (slot) slot.todo = action.payload.todo
    },
    // Live MCP session-report delta, same merge discipline as sseTodoUpdate. A
    // null payload is meaningful and must be stored: it is what the gateway
    // pushes when a session reset makes the previous report describe a session
    // that no longer exists, and keeping the old value would leave a dead
    // session's server list on screen as the live one's.
    sseMcpReportUpdate(
      state,
      action: PayloadAction<{ slot: string; mcp_report: McpSessionReport | null }>,
    ) {
      const slot = (state.slots ?? []).find(s => s.key === action.payload.slot)
      if (slot) slot.mcp_report = action.payload.mcp_report
    },
    // Bump a slot's recency timestamps on live message activity so the sidebar
    // re-ranks immediately off the finer-grained chat_message stream (vs waiting
    // for the next full sseSlots push). `last_ts` is the last message of any role,
    // so it moves for agent output too. `last_turn_ts` — the key the list is
    // ORDERED by — moves only when `settled` is set (an inbound prompt), because a
    // list that re-ranks on every streamed tool call swaps rows under the pointer
    // while several sessions work. A turn ENDING re-ranks via the slots push that
    // already carries the running-flag flip.
    //
    // Neither field may move BACKWARDS: an authoritative slots snapshot can land
    // between a caller buffering the event and dispatching it, and overwriting
    // that with an older arrival time reorders the sidebar. The two are guarded
    // separately because mid-turn `last_ts` is ahead of `last_turn_ts`, so a
    // shared check would discard a legitimate settling bump. Reducer stays pure —
    // the caller supplies ts (falling back to now at the dispatch site).
    touchSlotActivity(state, action: PayloadAction<{ key: string; ts: string; settled?: boolean }>) {
      const { key, ts, settled } = action.payload
      const slot = state.slots.find(s => s.key === key)
      if (!slot) return
      const t = Date.parse(ts)
      if (!slot.last_ts || Date.parse(slot.last_ts) <= t) slot.last_ts = ts
      if (settled && (!slot.last_turn_ts || Date.parse(slot.last_turn_ts) <= t)) slot.last_turn_ts = ts
    },
    setChannelTrusted(state, action: PayloadAction<boolean>) { state.channelTrusted = action.payload },
    sseSlotTitle(state, action: PayloadAction<{ key: string; title: string }>) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (slot) slot.title = action.payload.title
    },
    addSlotOptimistic(state, action: PayloadAction<ChatSlot>) {
      if (!state.slots.find(s => s.key === action.payload.key)) {
        state.slots.push(action.payload)
      }
    },
    removeSlotOptimistic(state, action: PayloadAction<string>) {
      state.slots = state.slots.filter(s => s.key !== action.payload)
      state.unreadSlots = state.unreadSlots.filter(k => k !== action.payload)
      if (state.unreadSince?.[action.payload] !== undefined) {
        const wasManual = state.unreadSince[action.payload] === MANUAL_UNREAD
        delete state.unreadSince[action.payload]
        if (wasManual) persistManualSentinels(state.unreadSince)
      }
      persistSharedUnread({}, [action.payload])
      _relayUnreadToParent(JSON.stringify(state.unreadSlots))
    },
    updateSlot(state, action: PayloadAction<Partial<ChatSlot> & { key: string }>) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (slot) Object.assign(slot, action.payload)
    },
    // Patch the sidebar's PR/MR chips (rendered from `slot.source_links`, the
    // Redux slots payload) from a `source_status` websocket delta. Without this
    // the delta only updated the react-query caches (Changes strip + detail
    // panel), leaving the sidebar chip on its pre-change glyph until an
    // unrelated slots broadcast happened by — the exact chip-vs-panel divergence
    // this feature exists to remove, recreated on the sidebar surface. The delta
    // is keyed by URL and may touch any slot that links that PR.
    patchSlotSourceLinks(
      state,
      action: PayloadAction<{ url: string; state?: NonNullable<ChatSlot['source_links']>[number]['state']; ci?: NonNullable<ChatSlot['source_links']>[number]['ci'] }>,
    ) {
      const { url } = action.payload
      if (!url) return
      for (const slot of state.slots) {
        if (!slot.source_links) continue
        for (const link of slot.source_links) {
          if (link.url !== url) continue
          if (action.payload.state !== undefined) link.state = action.payload.state
          if (action.payload.ci !== undefined) link.ci = action.payload.ci
        }
      }
    },
    /**
     * Patch ONE channel's link row, against whatever is in the store right now.
     *
     * The channel menu's callbacks must not rebuild the whole `links` array from
     * the array their render closed over: with two toggles in flight at once
     * (Slack and Discord, say) both derive from the same pre-mutation snapshot, so
     * the second dispatch overwrites the first and the sibling row silently
     * reverts until the next slots push corrects it. Each row is independently
     * mutable by design — one row per channel — so the store operation is per-row
     * too, which makes losing a sibling impossible rather than merely unlikely.
     *
     * Matched on channel PLUS `origin` when the caller supplies it. A session can
     * hold two deliveries on one channel at once — the conversation it was born in
     * and an explicit mirror to that same channel — and those mute separately, so
     * channel alone is ambiguous and picked whichever row came first. The
     * predicate here is deliberately the same one the caller used to choose the
     * endpoint's flag (`direction === 'origin'`), not equality against `direction`,
     * so a `'both'` row is classified identically on both sides. Callers with only
     * one possible row for the channel (Slack) may omit it. `patch` leaves a row
     * that does not exist alone rather than inventing one: an invented row cannot
     * know `paused`, which is how a disconnected channel came to render as
     * connected.
     */
    patchSlotLink(
      state,
      action: PayloadAction<{
        key: string
        channel: string
        origin?: boolean
        patch: Partial<NonNullable<ChatSlot['links']>[number]>
      }>,
    ) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (!slot?.links) return
      const wantOrigin = action.payload.origin
      const row = slot.links.find(candidate => (
        candidate.channel === action.payload.channel
        && (wantOrigin === undefined || (candidate.direction === 'origin') === wantOrigin)
      ))
      if (row) Object.assign(row, action.payload.patch)
    },
    updateSlotFolder(state, action: PayloadAction<{ key: string; folderId: string }>) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (slot) slot.folder_id = action.payload.folderId || undefined
    },
    updateSlotPin(state, action: PayloadAction<{ key: string; pinned: boolean }>) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (slot) {
        slot.pinned = action.payload.pinned
        state.slotPinGenerations ??= {}
        state.slotPinGenerations[action.payload.key] = (state.slotPinGenerations[action.payload.key] ?? 0) + 1
      }
    },
    triggerRefresh(state) { state.refreshTrigger += 1 },
    /** DUAL PAYLOAD SHAPE — the form IS the semantics. String payload =
     *  MANUAL reminder: records the relay-immune sentinel; only a local read
     *  in this window clears it. Object payload `{slot, ts?}` = message
     *  arrival: records a clearable watermark. Passing a bare string for an
     *  arrival creates a badge no remote read can retire — arrival call
     *  sites must always use the object form. */
    markSlotUnread(state, action: PayloadAction<string | { slot: string; ts?: string }>) {
      const slot = typeof action.payload === 'string' ? action.payload : action.payload.slot
      const ts = typeof action.payload === 'string' ? undefined : action.payload.ts
      if (!state.unreadSlots.includes(slot)) state.unreadSlots.push(slot)
      if (!state.unreadSince) state.unreadSince = {}  // partial preloaded state
      // Watermarks carry only ACTUAL server-minted message timestamps: a
      // frame without one falls back to the slot's last_ts, and when neither
      // exists nothing is recorded (any relayed read may clear). Minting
      // client time here would make windows disagree about the same message
      // and strand badges against valid relays.
      const effectiveTs = typeof action.payload === 'string'
        ? undefined
        : (ts ?? state.slots.find(s => s.key === slot)?.last_ts)
      if (typeof action.payload !== 'string') {
        // ONE atomic shared write: badge presence and watermark are the same
        // record entry ('' = badge with no watermark, any relayed read
        // clears). The RMW keeps the newest instant, so publishing on every
        // arrival converges.
        persistSharedUnread({ [slot]: effectiveTs ?? '' }, [])
        const prev = state.unreadSince[slot]
        // A manual sentinel is never demoted by a message arrival; otherwise
        // the chronologically newest parseable instant wins.
        if (effectiveTs !== undefined && prev !== MANUAL_UNREAD) {
          const next = newerTs(prev, effectiveTs)
          if (next !== undefined && next !== prev) state.unreadSince[slot] = next
        }
      } else {
        // Manual mark-as-unread: per-tab ONLY. The sentinel means NO remote
        // clear can meet the bar, and its badge never publishes to the
        // shared store — one window's private reminder must not surface in
        // every sibling.
        state.unreadSince[slot] = MANUAL_UNREAD
        persistManualSentinels(state.unreadSince)
      }
      _relayUnreadToParent(JSON.stringify(state.unreadSlots))
    },
    markSlotRead(state, action: PayloadAction<string>) {
      if (state.unreadSince?.[action.payload] !== undefined) {
        const wasManual = state.unreadSince[action.payload] === MANUAL_UNREAD
        delete state.unreadSince[action.payload]
        if (wasManual) persistManualSentinels(state.unreadSince)
      }
      // No-op guard: relayed slot_read frames fan in from every window (own
      // echo included); skipping absent keys keeps echo fan-in from
      // multiplying localStorage writes.
      if (!state.unreadSlots.includes(action.payload)) return
      state.unreadSlots = state.unreadSlots.filter(k => k !== action.payload)
      // The LOCAL badge always clears — the user read what this window
      // displayed. SHARED state clears only when this window's newest known
      // message (slot last_ts) covers the persisted shared watermark: a
      // lagging window (reconnect gap) cannot prove it saw the message a
      // sibling watermarked, so the shared badge survives for siblings and
      // reboots instead of being silently erased.
      const _readTs = state.slots?.find(sl => sl.key === action.payload)?.last_ts
      clearSharedUnreadIfCovered(action.payload, _readTs)
      _relayUnreadToParent(JSON.stringify(state.unreadSlots))
    },
    /** A read relayed from ANOTHER window: honors the watermark. Clears only
     *  when the relay's `readTs` covers everything that lit the badge here —
     *  a badge with no watermark (none was ever minted) accepts any relay,
     *  the MANUAL_UNREAD sentinel accepts none, and a newer local ts keeps
     *  the badge for the message the reader had not seen. Watermarks survive
     *  reload with their badges, so a restored badge keeps its guard against
     *  a sibling window's trailing relay. */
    remoteSlotRead(state, action: PayloadAction<{ slot: string; readTs?: string }>) {
      const { slot, readTs } = action.payload
      const since = state.unreadSince?.[slot]
      if (since !== undefined && !readCovers(readTs, since)) return
      // The local watermark accepted the relay — but this window's view can
      // be stale (reconnect gap), so the SHARED clear is guarded by the
      // shared map's own value, read inside the RMW. A sibling's newer
      // watermark survives, and this window adopts it: badge stays lit for
      // the message the relay did not cover.
      const survivor = clearSharedUnreadIfCovered(slot, readTs)
      if (survivor !== undefined) {
        if (!state.unreadSince) state.unreadSince = {}
        state.unreadSince[slot] = survivor
        if (!state.unreadSlots.includes(slot)) state.unreadSlots.push(slot)
        _relayUnreadToParent(JSON.stringify(state.unreadSlots))
        return
      }
      if (state.unreadSince?.[slot] !== undefined) {
        // A sentinel never reaches here (readCovers rejects it above), so the
        // deleted key is always a shared message watermark.
        delete state.unreadSince[slot]
      }
      if (!state.unreadSlots.includes(slot)) return
      state.unreadSlots = state.unreadSlots.filter(k => k !== slot)
      _relayUnreadToParent(JSON.stringify(state.unreadSlots))
    },
    setUpdateProgress(state, action: PayloadAction<{ step: string; detail: string } | null>) {
      state.updateProgress = action.payload
    },
    setDesktopUpdateAvailable(state, action: PayloadAction<boolean>) {
      state.desktopUpdateAvailable = action.payload
    },
    sseSubagentStatus(state, action: PayloadAction<{ running: number; slot: string; agents?: SubagentDetail[] }>) {
      const { slot, running, agents } = action.payload
      // `slot` is an untrusted key from the SSE payload; __proto__/constructor/
      // prototype would write through Object.prototype in the else-branch below.
      if (!slot || isUnsafeKey(slot)) return
      if (running <= 0) {
        evictSlotSubagents(state, slot)
      } else {
        state.subagentRunning[slot] = running
        if (agents) state.subagentDetails[slot] = agents.map(a => ({
          ...a,
          agent: sanitizeLlmOutput(a.agent || ''),
          last_tool: sanitizeLlmOutput(a.last_tool || ''),
          task: sanitizeLlmOutput(a.task || ''),
        }))
      }
    },
    sseSubagentText(state, action: PayloadAction<{ slot: string; id: string; text: string }>) {
      const { slot, id, text } = action.payload
      // Both `slot` and `id` are untrusted keys from the SSE payload. A value of
      // __proto__/constructor/prototype would pollute Object.prototype via the
      // `state.subagentText[slot][id] = ...` assignment below — and the
      // `subagentRunning[slot]` check does NOT stop `slot="__proto__"` because
      // it resolves truthily through the prototype chain. Guard both keys.
      if (isUnsafeKey(slot) || isUnsafeKey(id)) return
      if (!slot || !state.subagentRunning[slot]) return
      if (!state.subagentText[slot]) state.subagentText[slot] = {}
      const cur = (state.subagentText[slot][id] || '') + sanitizeLlmOutput(text)
      state.subagentText[slot][id] = cur.length > 4096 ? cur.slice(-4096) : cur
    },
    sseSlotColor(state, action: PayloadAction<{ key: string; color_index?: number | null; color_hex?: string | null }>) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (!slot) return
      // Mirror the backend's mutual exclusion: a non-null value for either
      // field clears the other, so optimistic updates can't leave a slot
      // carrying both.
      if ('color_index' in action.payload) {
        slot.color_index = action.payload.color_index ?? null
        if (slot.color_index !== null) slot.color_hex = null
      }
      if ('color_hex' in action.payload) {
        slot.color_hex = action.payload.color_hex ?? null
        if (slot.color_hex !== null) slot.color_index = null
      }
    },
    setSessionDefaultColor(state, action: PayloadAction<DefaultColorSetting>) {
      state.sessionDefaultColor = action.payload
      safeSet('mc-session-default-color', JSON.stringify(action.payload))
    },
    setSessionColorsMode(state, action: PayloadAction<SessionColorMode>) {
      state.sessionColorsMode = action.payload
      safeSet('mc-session-colors-mode', action.payload)
    },
    setSessionColorsPalette(state, action: PayloadAction<PaletteName>) {
      state.sessionColorsPalette = action.payload
      safeSet('mc-session-colors-palette', action.payload)
    },
    setSessionColorsIntensity(state, action: PayloadAction<IntensityName>) {
      state.sessionColorsIntensity = action.payload
      safeSet('mc-session-colors-intensity', action.payload)
    },
    setEnabledAppIds(state, action: PayloadAction<string[]>) {
      state.enabledAppIds = action.payload
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(fetchSlots.fulfilled, (state, action) => {
        // A reply in flight can be older than the live frames that arrived while
        // it travelled, so it may omit a slot the stream has since created. The
        // unread drain still runs — that is this path's documented job, and a
        // badge self-heals — but eviction is withheld once the stream is live.
        const fresh = !state.slotsLoaded
        applySlots(state, action.payload)
        state.slotsGeneration = (state.slotsGeneration ?? 0) + 1
        state.slotsLoaded = true
        reconcileSlots(state, new Set(action.payload.map((s: { key: string }) => s.key)), fresh)
      })
      .addCase(changeApprovalMode.fulfilled, (state, action) => { state.approvalMode = action.payload })
      // The created slot joins the list on the SAME action that activates it
      // (chatSlice's createSlot.fulfilled), so the sidebar row and the empty
      // transcript land in one commit. A separate optimistic dispatch ahead of
      // `fulfilled` would render the new row over the OLD chat for a frame and
      // charge the sidebar its insertion render twice. Matched by type string
      // rather than importing the thunk: chatSlice imports this slice, and a
      // cycle here breaks module init. Idempotent by key, because the live
      // `slots` frame announcing the slot usually arrives before the create
      // response, so the row is often already present.
      .addMatcher(
        (action): action is PayloadAction<ChatSlot> => action.type === 'chat/createSlot/fulfilled',
        (state, action) => {
          if (!state.slots.find(s => s.key === action.payload.key)) {
            state.slots.push(action.payload)
          }
        },
      )
  },
})

export const { sseStatus, sseYolo, setYoloDuration, sseConnected, sseDisconnected, sseSlots, setSidebarOrder, sseTodoUpdate, sseMcpReportUpdate, touchSlotActivity, setChannelTrusted, sseSlotTitle, addSlotOptimistic, removeSlotOptimistic, updateSlot, updateSlotFolder, updateSlotPin, triggerRefresh, markSlotUnread, markSlotRead, remoteSlotRead, setUpdateProgress,
  setDesktopUpdateAvailable, sseSubagentStatus, sseSubagentText, sseSlotColor, setSessionDefaultColor, setSessionColorsMode, setSessionColorsPalette, setSessionColorsIntensity, setEnabledAppIds, patchSlotSourceLinks, patchSlotLink } = dashboardSlice.actions

/**
 * Resolve a slot's surface key. Backend emits `surface` (mirrors `mode` today
 * but lets the two diverge later); fall back to `mode` for slots delivered
 * before the backend rollout. Empty string is the canonical "main chat" key.
 */
export function slotSurfaceKey(slot: { mode?: string; surface?: string }): string {
  return slot.surface ?? slot.mode ?? ''
}

/**
 * Count unread slots whose surface matches `mode`. Slots present in
 * `unreadSlots` but missing from `slots` (e.g. deleted but not yet drained)
 * are treated as the default chat surface (`""`) so they keep contributing
 * to the Chat badge rather than vanishing silently.
 *
 * Note — intentional asymmetry with `filterUnreadKeysBySurface` in
 * `surfaces/registry.ts`: that helper drops orphan keys (the sidebar can't
 * display them regardless), whereas this one keeps them so the badge stays
 * stable across the brief race between `removeSlotOptimistic` and
 * `fetchSlots.fulfilled`.
 */
function countUnreadByMode(slots: ChatSlot[], unread: string[], mode: string): number {
  if (unread.length === 0) return 0
  const surfaceByKey = new Map(slots.map(s => [s.key, slotSurfaceKey(s)]))
  // Unified chat: when counting for the chat surface (''), include orchestrator
  // slots too since they now live in the same sidebar.
  const isChatSurface = mode === ''
  let count = 0
  for (const k of unread) {
    const sk = surfaceByKey.get(k) ?? ''
    if (isChatSurface ? (sk === '' || sk === 'orchestrator') : sk === mode) count++
  }
  return count
}

/**
 * Memoized factory for "unread count for slots whose surface === mode".
 * One memo cache per `mode` argument so registry surfaces don't trash each
 * other's memoization. Built-in nav badges should not call this directly —
 * they go through `selectSurfaceBadgeCount(navId)` from `surfaces/registry`,
 * which routes to this factory only when a surface declares `slotMode`.
 */
type UnreadByModeSelector = (state: { dashboard: DashboardState }) => number
const _unreadByModeCache = new Map<string, UnreadByModeSelector>()
export function selectUnreadByMode(mode: string): UnreadByModeSelector {
  let sel = _unreadByModeCache.get(mode)
  if (!sel) {
    sel = createSelector(
      (state: { dashboard: DashboardState }) => state.dashboard.slots,
      (state: { dashboard: DashboardState }) => state.dashboard.unreadSlots,
      (slots, unread) => countUnreadByMode(slots, unread, mode),
    )
    _unreadByModeCache.set(mode, sel)
  }
  return sel
}

export default dashboardSlice.reducer
