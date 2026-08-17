import { safeSetItem } from '../utils/safeStorage'
import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { sanitizeLlmOutput, isUnsafeKey } from '../utils/sanitize'
import type { StatusData, ChatSlot, TodoList } from '../types'
import type { SessionColorMode, PaletteName, DefaultColorSetting, IntensityName } from '../utils/sessionColors'

export interface SubagentDetail {
  id: string; task: string; agent: string; turns: number; last_tool: string; startedAt: number
}

interface DashboardState {
  status: StatusData | null
  connected: boolean
  slots: ChatSlot[]
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
    sseConnected(state) { state.connected = true; state.slotsLoaded = false; state.subagentRunning = {}; state.subagentDetails = {}; state.subagentText = {} },
    sseDisconnected(state) { state.connected = false },
    sseSlots(state, action: PayloadAction<ChatSlot[]>) { state.slots = action.payload; state.slotsLoaded = true },
    // Live TODO-list delta. Patched into the SAME slots array that sseSlots
    // populates rather than a parallel map, so the mid-turn push and the
    // reconnect snapshot can never disagree about a slot's list. A delta for an
    // unknown slot is dropped — the next sseSlots push carries it anyway.
    sseTodoUpdate(state, action: PayloadAction<{ slot: string; todo: TodoList | null }>) {
      const slot = (state.slots ?? []).find(s => s.key === action.payload.slot)
      if (slot) slot.todo = action.payload.todo
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
        delete state.subagentRunning[slot]
        delete state.subagentDetails[slot]
        delete state.subagentText[slot]
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
    sseSlotColor(state, action: PayloadAction<{ key: string; color_index: number | null }>) {
      const slot = state.slots.find(s => s.key === action.payload.key)
      if (slot) slot.color_index = action.payload.color_index
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
        state.slots = action.payload
        state.slotsLoaded = true
        const liveKeys = new Set(action.payload.map((s: { key: string }) => s.key))
        state.unreadSlots = state.unreadSlots.filter(k => liveKeys.has(k))
        safeSet('mc-unread-slots', JSON.stringify(state.unreadSlots))
      })
      .addCase(changeApprovalMode.fulfilled, (state, action) => { state.approvalMode = action.payload })
  },
})

export const { sseStatus, sseConnected, sseDisconnected, sseSlots, sseTodoUpdate, touchSlotActivity, setChannelTrusted, sseSlotTitle, addSlotOptimistic, removeSlotOptimistic, updateSlot, updateSlotFolder, updateSlotPin, triggerRefresh, markSlotUnread, markSlotRead, setUpdateProgress,
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
