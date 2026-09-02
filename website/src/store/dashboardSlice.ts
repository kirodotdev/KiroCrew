import { safeSetItem } from '../utils/safeStorage'
import { jsonEqual } from '../utils/structuralEqual'
import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { sanitizeLlmOutput, isUnsafeKey } from '../utils/sanitize'
import type { StatusData, ChatSlot, TodoList, McpSessionReport } from '../types'
import type { SessionColorMode, PaletteName, DefaultColorSetting, IntensityName } from '../utils/sessionColors'

export interface SubagentDetail {
  id: string; task: string; agent: string; turns: number; last_tool: string; startedAt: number
}

interface DashboardState {
  status: StatusData | null
  connected: boolean
  slots: ChatSlot[]
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
  sidebarOrder: [],
  approvalMode: 'normal',
  channelTrusted: false,
  refreshTrigger: 0,
  unreadSlots: (() => { try { return JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]') as string[] } catch { return [] } })(),
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

export const changeApprovalMode = createAsyncThunk(
  'dashboard/changeApprovalMode',
  async ({ mode, slot }: { mode: string; slot?: string }) => {
    await api.chatMode(mode, slot)
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
    state.unreadSlots = drained
    safeSet('mc-unread-slots', JSON.stringify(drained))
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
    sseStatus(state, action: PayloadAction<StatusData>) {
      state.status = action.payload
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
      safeSet('mc-unread-slots', JSON.stringify(state.unreadSlots))
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
      if (slot) slot.pinned = action.payload.pinned
    },
    triggerRefresh(state) { state.refreshTrigger += 1 },
    markSlotUnread(state, action: PayloadAction<string>) {
      if (!state.unreadSlots.includes(action.payload)) state.unreadSlots.push(action.payload)
      safeSet('mc-unread-slots', JSON.stringify(state.unreadSlots))
    },
    markSlotRead(state, action: PayloadAction<string>) {
      state.unreadSlots = state.unreadSlots.filter(k => k !== action.payload)
      safeSet('mc-unread-slots', JSON.stringify(state.unreadSlots))
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
        state.slotsLoaded = true
        reconcileSlots(state, new Set(action.payload.map((s: { key: string }) => s.key)), fresh)
      })
      .addCase(changeApprovalMode.fulfilled, (state, action) => { state.approvalMode = action.payload })
  },
})

export const { sseStatus, sseYolo, sseConnected, sseDisconnected, sseSlots, setSidebarOrder, sseTodoUpdate, sseMcpReportUpdate, touchSlotActivity, setChannelTrusted, sseSlotTitle, addSlotOptimistic, removeSlotOptimistic, updateSlot, updateSlotFolder, updateSlotPin, triggerRefresh, markSlotUnread, markSlotRead, setUpdateProgress,
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
