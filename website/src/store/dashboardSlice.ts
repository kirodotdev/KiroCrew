import { safeSetItem, safeSetSessionItem } from '../utils/safeStorage'
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

/** A close tombstone entry (see `applySlots`). */
interface CloseHold {
  /** The `deleteSlot` attempt that owns this hold. A stale attempt's lifecycle
   *  actions (a `rejected` trailing a slow peer navigation) must not touch a
   *  newer attempt's hold on the same key. */
  requestId: string
  /** Deadline while the DELETE is in flight; `null` once the server has answered. */
  inFlightUntil: number | null
  /** Straggler frames the hold survives after confirmation. */
  graceFrames: number
  /** `fetchSlots` requests that were already in flight when the server
   *  confirmed the close. Their replies may predate the pop, so the hold
   *  outlives them however many frames arrive first. */
  awaitingFetches: string[]
  /** Wall-clock deadline for the confirmed phase. `chatSlots()` carries no
   *  deadline either, so an `awaitingFetches` entry that never settles would
   *  otherwise pin the tombstone until a reload. Set at confirmation. */
  confirmedUntil: number | null
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
  /** Keys whose close is in flight or just confirmed, held out of `slots` until an
   *  authoritative list omits them. See `applySlots` for why membership alone is
   *  not enough: the server still lists a slot whose DELETE has not finished.
   *  Bounded twice over — by the wall clock while the request is in flight, and
   *  by a small frame budget after the server has confirmed. */
  closingSlots: Record<string, CloseHold>
  /** `fetchSlots` requests currently in flight, by thunk requestId — read by
   *  a confirmed close to know which replies could still predate its pop. */
  slotFetchesInFlight: string[]
  /** `fetchSlots` requests a confirmed close was still waiting on when its
   *  wall-clock cap expired, mapped to the closed keys each must not restore.
   *  Such a reply is known to predate those pops, so on arrival the closed rows
   *  are filtered out of it and the REST of the payload still applies — an
   *  unrelated change it carries is not thrown away. Entries leave when the
   *  request settles. */
  staleSlotFetches: Record<string, string[]>
  /** Monotonic counter of single-slot row writes, bumped by `patchSlotRow` —
   *  the one path by which a reducer may change a field of an existing row.
   *  Ordering is all this needs to express, so a counter is used rather than a
   *  clock: two writes in the same millisecond must still be distinguishable,
   *  and no wall clock is involved in comparing them. */
  slotWriteSeq: number
  /** Per key, the value of `slotWriteSeq` at that row's last single-slot write.
   *  Compared against a `fetchSlots` request's own mark to decide whether the
   *  reply predates what is on screen for that key. Pruned with the rest of the
   *  per-slot state when an authoritative frame drops the key. */
  slotWrittenAt: Record<string, number>
  /** Per in-flight `fetchSlots` requestId, the value of `slotWriteSeq` when the
   *  request was dispatched. The server serialized its reply after that instant,
   *  so any key whose `slotWrittenAt` is HIGHER was written locally while the
   *  reply travelled and the reply is older than the screen for that key.
   *  Entries leave when the request settles. */
  slotFetchWriteMark: Record<string, number>
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
    // safeSetItem, not a raw setItem: this write runs on the websocket
    // onmessage -> dispatch -> re-render path, so a QuotaExceededError here
    // would escape a React ErrorBoundary and white-screen the app. The helper
    // reclaims a disposable tier and retries, so the record survives a full
    // quota instead of being dropped beside reclaimable cache.
    //
    // The return value is load-bearing, and it is the one thing a plain
    // conversion from the old raw call loses. The raw setItem THREW on a full
    // quota, so the surrounding catch swallowed it and the projection write
    // below never ran. A helper that reports the same failure by returning
    // false does not stop the function, and the projection is strictly smaller
    // than the record (keys only, no timestamps) — so on a quota it can free
    // space and succeed where the record just failed, leaving the two persisted
    // records disagreeing. restoreUnreadSince trusts the record; older tabs and
    // the hub relay read the projection. Bail out before that can happen.
    if (!safeSetItem('mc-unread-shared', JSON.stringify(stored))) return
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
  safeSetSessionItem('mc-unread-since', JSON.stringify(manual))
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
      if (legacy.length > 0) safeSetItem('mc-unread-shared', JSON.stringify(record))
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
  closingSlots: {},
  slotFetchesInFlight: [],
  staleSlotFetches: {},
  slotWriteSeq: 0,
  slotWrittenAt: {},
  slotFetchWriteMark: {},
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
  // Same guard, same reason: a write stamp for a key the authoritative list has
  // dropped protects nothing, and keeping it would let the record grow for the
  // tab's lifetime. Pruned only when the caller vouches for the list, so an HTTP
  // reply that merely predates a newly created slot cannot strip its protection.
  for (const stamped of Object.keys(state.slotWrittenAt ?? {})) {
    if (!liveKeys.has(slotKeyOfStamp(stamped))) delete state.slotWrittenAt[stamped]
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
 *  an equal result.
 *
 *  Membership has ONE exception: a key in `closingSlots`. `deleteSlot` removes
 *  the row before its DELETE round-trips, but the server keeps the slot in its
 *  registry across several awaits (nudge-loop retirement, the app hook, the
 *  history save) before popping it, and slot pushes coalesce on a 200 ms window
 *  that re-serializes at delivery time — so a frame listing the closing slot as
 *  live is ordinary, not rare, and an HTTP `/api/chat/slots` reply can predate
 *  the click outright. Taking membership at face value put the row back, and
 *  the first post-pop frame removed it again: the disappear / reappear /
 *  disappear flicker of #11224. A closing key is held out of `slots` for the
 *  life of the tombstone. Everything else about the frame — the other rows, the
 *  generation bump, the unread drain — still applies, so the hold never delays
 *  anything but the one row being dismissed.
 *
 *  A list that OMITS the key does not release the hold. It would be tempting to
 *  read that as the server's confirmation, but the two authoritative writers
 *  are not ordered with each other: an HTTP `/api/chat/slots` reply can be
 *  older than the live frames that arrived while it travelled (see
 *  `fetchSlots.fulfilled`), so a post-pop frame that omitted the key could
 *  release the hold and a pre-pop reply still in flight would then re-add the
 *  row. The tombstone is instead retired by the close's OWN lifecycle: while
 *  the DELETE is in flight it holds unconditionally, and once the server has
 *  answered it survives a small, fixed budget of further authoritative lists
 *  (listing the key or not — a straggler can only be that far behind) and then
 *  yields to membership. A server that keeps listing a slot it claims to have
 *  closed is a server bug, and hiding its row indefinitely would turn that bug
 *  into a session the user can no longer see; the budget bounds that too.
 *
 *  "The request bounds it" needs one caveat: `deleteChatSlot` carries no
 *  deadline, so a DELETE stalled server-side (the app hook or the history save
 *  hanging on exactly the long-context session that made the close slow) would
 *  otherwise hold the row hidden until a reload. The in-flight hold therefore
 *  also expires on the wall clock: past `CLOSE_IN_FLIGHT_MAX_MS` the next
 *  authoritative list applies as-is, so a frame that still lists the key shows
 *  it again. That is the pre-fix behaviour — a row that visibly came back —
 *  which is the right failure mode for a close that has not been confirmed in
 *  that long. The confirmed phase has the same cap (`CLOSE_CONFIRMED_MAX_MS`)
 *  for the same reason: `chatSlots()` carries no deadline either, so a
 *  `fetchSlots` snapshotted into `awaitingFetches` that never settles must not
 *  pin the tombstone until a reload. Unlike the in-flight case, though, the
 *  server HAS confirmed this close, so a reply from one of those fetches that
 *  finally lands is known to be pre-pop for THAT key: the pairing is recorded
 *  in `staleSlotFetches` at expiry and the key is filtered out of the reply on
 *  arrival (see `fetchSlots.fulfilled`) instead of resurrecting the row, while
 *  the rest of the reply still applies. */
/** Wall-clock cap on the in-flight hold; well past any healthy close, short of a reload. */
const CLOSE_IN_FLIGHT_MAX_MS = 30_000
/** Wall-clock cap on the confirmed hold: however many straggler lists or
 *  unsettled pre-confirmation fetches remain, the row is shown again past this
 *  if the server still lists it. A pre-pop reply cannot legitimately trail the
 *  pop by anything like this long; one that does has stalled, and a stalled
 *  request is the same failure the in-flight cap exists for. */
const CLOSE_CONFIRMED_MAX_MS = 30_000
/** Authoritative lists a confirmed close's tombstone outlives before yielding
 *  to membership. Live pushes coalesce on 200 ms, so a frame serialized before
 *  the pop is among the first few after the 200. HTTP replies are not bounded
 *  by this budget: a `fetchSlots` that was in flight at the 200 is tracked by
 *  requestId on the hold and the hold outlives its settlement outright. */
const CLOSE_CONFIRMED_GRACE_FRAMES = 3
/** Drop one close tombstone, but only the attempt that owns it: a stale
 *  attempt's `rejected` must not release a retry's hold on the same key.
 *  Prototype keys are refused at the writer, so they can never be present;
 *  skipping them here keeps every dynamic access to the record behind the same
 *  guard. */
const releaseHold = (state: DashboardState, key: string, requestId: string): void => {
  if (isUnsafeKey(key)) return
  if (state.closingSlots?.[key]?.requestId === requestId) delete state.closingSlots[key]
}
/** Record that the fetches a confirmed hold was still awaiting at expiry must
 *  not restore `key` when their replies land. */
const markStaleSlotFetches = (state: DashboardState, key: string, requestIds: string[]): void => {
  if (!requestIds.length) return
  if (!state.staleSlotFetches) state.staleSlotFetches = {}
  for (const id of requestIds) {
    const keys = state.staleSlotFetches[id] ?? []
    if (!keys.includes(key)) state.staleSlotFetches[id] = [...keys, key]
  }
}
/** The closed keys a settling `fetchSlots` reply must not restore. Either the
 *  expiry branch in `applySlots` already recorded them, or a confirmed hold is
 *  STILL awaiting this request and has passed its own deadline — in which case
 *  this very reply would be the first list to trigger that expiry, and it must
 *  not be the one that puts the row back. Expire the hold here so the key is
 *  filtered like any other stale pairing. */
const staleKeysForSlotFetch = (state: DashboardState, requestId: string): Set<string> => {
  const now = Date.now()
  const closing = state.closingSlots ?? {}
  for (const key of Object.keys(closing)) {
    const hold = closing[key]
    if (hold.confirmedUntil === null || now < hold.confirmedUntil || !hold.awaitingFetches.includes(requestId)) continue
    markStaleSlotFetches(state, key, hold.awaitingFetches)
    delete closing[key]
  }
  return new Set(state.staleSlotFetches?.[requestId] ?? [])
}
/** Move a hold from the in-flight clock to the confirmed phase. Called from the
 *  thunk the moment the DELETE resolves (`confirmCloseHold`), NOT from the
 *  thunk's `fulfilled`: that action trails an unbounded peer navigation, and a
 *  successful close whose navigation outlasted the in-flight cap would
 *  otherwise expire as if it had stalled. `fulfilled` still calls this as
 *  belt-and-braces; the second call is a no-op. */
const confirmHold = (state: DashboardState, key: string, requestId: string): void => {
  const hold = isUnsafeKey(key) ? undefined : state.closingSlots?.[key]
  if (!hold || hold.requestId !== requestId || hold.inFlightUntil === null) return
  hold.inFlightUntil = null
  hold.awaitingFetches = [...(state.slotFetchesInFlight ?? [])]
  hold.confirmedUntil = Date.now() + CLOSE_CONFIRMED_MAX_MS
}
/** A `fetchSlots` request settled: no confirmed hold need wait for it any more.
 *  Runs AFTER that reply's own `applySlots`, so the reply itself is still held. */
const settleSlotFetch = (state: DashboardState, requestId: string | undefined): void => {
  if (!requestId) return
  state.slotFetchesInFlight = (state.slotFetchesInFlight ?? []).filter(id => id !== requestId)
  if (state.staleSlotFetches?.[requestId]) delete state.staleSlotFetches[requestId]
  if (state.slotFetchWriteMark?.[requestId] !== undefined) delete state.slotFetchWriteMark[requestId]
  const closing = state.closingSlots ?? {}
  for (const key of Object.keys(closing)) {
    const hold = closing[key]
    if (!hold.awaitingFetches.includes(requestId)) continue
    hold.awaitingFetches = hold.awaitingFetches.filter(id => id !== requestId)
    if (hold.inFlightUntil === null && hold.graceFrames === 0 && hold.awaitingFetches.length === 0) delete closing[key]
  }
}
/** `slotWrittenAt` is indexed through this, never by the bare slot key.
 *
 *  The prefix means no key — however it was minted, and whatever it normalizes
 *  to — can reach `Object.prototype`, so the record needs no `isUnsafeKey`
 *  guard, and a slot keyed `__proto__` or `constructor` is protected like any
 *  other instead of being skipped. That matters because the guard's whole value
 *  is that it has no exceptions: one unstamped write is one row where #11149
 *  still happens, and "unless the key is unusual" is not a property anyone can
 *  hold in their head while adding the eleventh writer. */
const stampKey = (slotKey: string): string => `k:${slotKey}`
/** Inverse of `stampKey`, for the two places that read the record back. */
const slotKeyOfStamp = (stamped: string): string => stamped.slice(2)

/** Record that `key`'s row was just written by a single-slot writer.
 *
 *  Only the ORDER of these writes against a `fetchSlots` dispatch matters, so a
 *  counter is the whole mechanism: no clock is read, and two writes in the same
 *  millisecond stay distinguishable. */
const stampSlotWrite = (state: DashboardState, key: string): void => {
  const seq = (state.slotWriteSeq ?? 0) + 1
  state.slotWriteSeq = seq
  if (!state.slotWrittenAt) state.slotWrittenAt = {}
  state.slotWrittenAt[stampKey(key)] = seq
}

/** A single-slot mutation. Returning `false` means the writer's own guard
 *  declined and the row was left alone, so no write is stamped. */
type RowPatch = (slot: ChatSlot) => boolean | void

/** THE way a reducer changes a field of an EXISTING row of `state.slots`.
 *
 *  This exists to make the `fetchSlots` clobber UNREPRESENTABLE rather than
 *  merely absent. A `/api/chat/slots` reply is serialized at the server and
 *  applied by `applySlots` as a whole-list positional replace, so a single-slot
 *  write that lands inside that round trip is overwritten by the older server
 *  row (issue #11149). The remedy needs a per-slot recency signal, and the only
 *  way a signal cannot be forgotten is for it to be the cost of reaching the
 *  row at all: a writer added later inherits the protection by construction
 *  instead of by review. `dashboardSlice.rowWriterChokepoint.test.ts` fails the
 *  build if a reducer reaches a row any other way without saying, on the line,
 *  that its lookup is read-only.
 *
 *  Deliberately NOT applied to membership changes (`addSlotOptimistic`,
 *  `removeSlotOptimistic`): the reply's own membership is reconciled by the
 *  close-tombstone machinery and by `applySlots`, and those two are already
 *  ordered against each other. This guards row CONTENT. */
const patchSlotRow = (state: DashboardState, key: string, patch: RowPatch): void => {
  const slot = (state.slots ?? []).find(s => s.key === key) // row-write: via patchSlotRow
  if (!slot) return
  if (patch(slot) === false) return
  stampSlotWrite(state, key)
}

/** `patchSlotRow` for a writer that is not keyed by slot: a `source_status`
 *  delta names a URL and may touch every row that links it. Same stamp, so a
 *  URL-keyed write is protected exactly like a key-keyed one. */
const patchSlotRowsWhere = (state: DashboardState, patch: RowPatch): void => {
  for (const slot of state.slots ?? []) { // row-write: via patchSlotRow
    if (patch(slot) === false) continue
    stampSlotWrite(state, slot.key)
  }
}

/** Keys of `incoming` that a `fetchSlots` reply must not overwrite, because a
 *  single-slot writer touched them after the request was dispatched — and the
 *  server therefore serialized this reply without knowing about that write.
 *
 *  `mark` is the request's own `slotWriteSeq` snapshot; a key stamped ABOVE it
 *  was written while the reply travelled. The comparison is deliberately
 *  conservative at one end: a write that landed between the dispatch and the
 *  server's serialization is counted too, because the client cannot tell those
 *  two instants apart without a server-minted stamp on the reply. The cost of
 *  that is bounded and self-correcting — the withheld row is re-delivered by the
 *  next authoritative frame (a live push, or the 5 s Worlds poll) — whereas a
 *  clobbered local write has no re-delivery path at all, which is the asymmetry
 *  that decides the direction to err in. */
const localWritesOutranking = (state: DashboardState, requestId: string | undefined): Set<string> => {
  const outranked = new Set<string>()
  if (requestId === undefined) return outranked
  const mark = state.slotFetchWriteMark?.[requestId]
  if (mark === undefined) return outranked
  const writtenAt = state.slotWrittenAt ?? {}
  for (const stamped of Object.keys(writtenAt)) {
    if (writtenAt[stamped] > mark) outranked.add(slotKeyOfStamp(stamped))
  }
  return outranked
}

const applySlots = (state: DashboardState, incomingRows: ChatSlot[]): void => {
  let next = incomingRows
  const closing = state.closingSlots ?? {}
  const closingKeys = Object.keys(closing)
  if (closingKeys.length) {
    const held = new Set<string>()
    const now = Date.now()
    for (const key of closingKeys) {
      const hold = closing[key]
      if (hold.inFlightUntil !== null) {
        if (now < hold.inFlightUntil) { held.add(key); continue }
        // Stalled past the cap: stop hiding a session nobody has confirmed gone.
        delete closing[key]
        continue
      }
      if (hold.confirmedUntil !== null && now >= hold.confirmedUntil) {
        // A pre-pop reply that has not landed by now has stalled; stop waiting
        // for it — but remember it, so that when it finally lands it is dropped
        // rather than applied as if it described the current registry.
        markStaleSlotFetches(state, key, hold.awaitingFetches)
        delete closing[key]
        continue
      }
      // Confirmed: every authoritative list, listing the key or not, spends
      // one unit of the frame budget. Counting omissions too is what keeps a
      // stale reply that lands AFTER an omitting frame from re-adding the row.
      // A reply from a fetch that predates the confirmation is held regardless
      // of the budget (see `awaitingFetches`): it is the one list that can be
      // arbitrarily late and still list the key.
      if (hold.graceFrames > 0) hold.graceFrames -= 1
      if (hold.graceFrames > 0 || hold.awaitingFetches.length > 0) { held.add(key); continue }
      // The budget's last list is still held; the entry just does not outlive
      // it, so a same-key session seen from the NEXT list on shows.
      held.add(key)
      delete closing[key]
    }
    if (held.size) next = incomingRows.filter(s => !held.has(s.key))
  }
  const prev = state.slots ?? []
  const byKey = new Map(prev.map(s => [s.key, s]))
  let changed = prev.length !== next.length
  const merged = next.map((incoming, i) => {
    const existing = byKey.get(incoming.key)
    // Reusing a draft row inside a freshly assigned array is fine: Immer
    // finalizes drafts found in the assigned value within the same scope, so an
    // untouched row resolves back to its base object and keeps its identity.
    // CONTRACT (leaned on cross-slice): a row keeps its object identity iff
    // it is jsonEqual to the incoming one; any changed or replaced row gets a
    // fresh object. chatSlice's switchSlot 404 eviction captures a row at
    // dispatch and treats a changed identity as "an authoritative frame
    // altered this row mid-flight" to disarm itself — see the catch in
    // switchSlot and switchSlotCallsiteClassification/rejection tests.
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
      patchSlotRow(state, action.payload.slot, slot => { slot.todo = action.payload.todo })
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
      patchSlotRow(state, action.payload.slot, slot => { slot.mcp_report = action.payload.mcp_report })
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
      patchSlotRow(state, key, slot => {
        const t = Date.parse(ts)
        let moved = false
        if (!slot.last_ts || Date.parse(slot.last_ts) <= t) { slot.last_ts = ts; moved = true }
        if (settled && (!slot.last_turn_ts || Date.parse(slot.last_turn_ts) <= t)) { slot.last_turn_ts = ts; moved = true }
        // A bump both guards rejected changed nothing, so it is not a write and
        // must not outrank a reply: stamping it would withhold a server row on
        // the strength of a no-op.
        return moved
      })
    },
    setChannelTrusted(state, action: PayloadAction<boolean>) { state.channelTrusted = action.payload },
    sseSlotTitle(state, action: PayloadAction<{ key: string; title: string }>) {
      patchSlotRow(state, action.payload.key, slot => { slot.title = action.payload.title })
    },
    addSlotOptimistic(state, action: PayloadAction<ChatSlot>) {
      // A resume or fork under a key that was closing supersedes the tombstone:
      // the caller has a fresh server acknowledgement that the key is live.
      if (!isUnsafeKey(action.payload.key)) delete state.closingSlots?.[action.payload.key]
      if (!state.slots.find(s => s.key === action.payload.key)) { // row-read: membership test, adds a row rather than changing one
        state.slots.push(action.payload)
      }
    },
    /** Drop a close tombstone (see `applySlots`). `deleteSlot` dispatches this in
     *  its failure path BEFORE the recovery `fetchSlots`, because the thunk's
     *  `rejected` action only fires after a trailing `await navigation` (a peer
     *  transcript load, unbounded), and the refetch reply must not be filtered
     *  by the very hold the failed close armed. */
    releaseCloseHold(state, action: PayloadAction<{ key: string; requestId: string }>) {
      releaseHold(state, action.payload.key, action.payload.requestId)
    },
    /** The DELETE resolved: the server has popped the slot. Dispatched by
     *  `deleteSlot` before it awaits the peer navigation (see `confirmHold`). */
    confirmCloseHold(state, action: PayloadAction<{ key: string; requestId: string }>) {
      confirmHold(state, action.payload.key, action.payload.requestId)
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
      patchSlotRow(state, action.payload.key, slot => { Object.assign(slot, action.payload) })
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
      patchSlotRowsWhere(state, slot => {
        if (!slot.source_links) return false
        let touched = false
        for (const link of slot.source_links) {
          if (link.url !== url) continue
          if (action.payload.state !== undefined) { link.state = action.payload.state; touched = true }
          if (action.payload.ci !== undefined) { link.ci = action.payload.ci; touched = true }
        }
        // A row that links no matching URL was not written, so it is not stamped
        // — otherwise one delta would outrank a reply for every slot on screen.
        return touched
      })
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
      patchSlotRow(state, action.payload.key, slot => {
        if (!slot.links) return false
        const wantOrigin = action.payload.origin
        const row = slot.links.find(candidate => (
          candidate.channel === action.payload.channel
          && (wantOrigin === undefined || (candidate.direction === 'origin') === wantOrigin)
        ))
        if (!row) return false
        Object.assign(row, action.payload.patch)
      })
    },
    updateSlotFolder(state, action: PayloadAction<{ key: string; folderId: string }>) {
      patchSlotRow(state, action.payload.key, slot => { slot.folder_id = action.payload.folderId || undefined })
    },
    updateSlotPin(state, action: PayloadAction<{ key: string; pinned: boolean }>) {
      patchSlotRow(state, action.payload.key, slot => {
        slot.pinned = action.payload.pinned
        state.slotPinGenerations ??= {}
        state.slotPinGenerations[action.payload.key] = (state.slotPinGenerations[action.payload.key] ?? 0) + 1
      })
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
        : (ts ?? state.slots.find(s => s.key === slot)?.last_ts) // row-read: reads a watermark, writes only unread state
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
      const _readTs = state.slots?.find(sl => sl.key === action.payload)?.last_ts // row-read: reads a watermark, writes only unread state
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
      patchSlotRow(state, action.payload.key, slot => {
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
      })
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
      .addCase(fetchSlots.pending, (state, action) => {
        if (!state.slotFetchesInFlight) state.slotFetchesInFlight = []
        state.slotFetchesInFlight.push(action.meta.requestId)
        // The reply cannot describe anything that happens from here on, so this
        // is the instant every later single-slot write outranks it from.
        if (!state.slotFetchWriteMark) state.slotFetchWriteMark = {}
        state.slotFetchWriteMark[action.meta.requestId] = state.slotWriteSeq ?? 0
      })
      .addCase(fetchSlots.fulfilled, (state, action) => {
        // A reply a confirmed close waited on past its cap is known to predate
        // that close's pop, so the closed key is filtered out of it — but ONLY
        // that key. Whatever else the reply carries (an unrelated rename, a
        // slot created meanwhile) is still the newest thing this transport has
        // said and still applies. Before the first live snapshot the reply is
        // the only list there is and applies unfiltered: an empty sidebar is
        // worse than one stale row.
        // `meta` is optional-chained for the hand-built actions in older tests.
        const requestId = action.meta?.requestId
        const staleKeys = requestId && state.slotsLoaded ? staleKeysForSlotFetch(state, requestId) : new Set<string>()
        // Keys a single-slot writer touched while this request was in flight.
        // The server serialized the reply before that write existed, so for
        // THOSE keys the reply is older than the screen and its row is dropped
        // in favour of the live one — which is the same substitution the stale
        // close above performs, for the same reason, one level finer.
        //
        // Deliberately NOT gated on `slotsLoaded`, unlike the stale-close filter
        // above. `sseConnected` clears that flag on every reconnect while
        // LEAVING the rows in place, and reconnect is the one caller that
        // refetches while the WS replay backlog writes into those same rows — so
        // gating here would switch the guard off precisely where the race is
        // most likely. A true cold boot needs no gate: `patchSlotRow` can only
        // stamp a row it found, an empty list has none, and the set is therefore
        // empty and the reply applies unfiltered.
        const outranked = localWritesOutranking(state, requestId)
        // For a stale key the reply's row is pre-pop and is NOT trusted, but the
        // key may since have been recreated (a resume on another client): keep
        // whatever row is on screen for it rather than dropping the key, so a
        // live same-key session and its unread state survive the stale reply.
        const current = staleKeys.size || outranked.size ? new Map((state.slots ?? []).map(s => [s.key, s])) : undefined
        const rows: ChatSlot[] = current
          ? action.payload.flatMap((s: ChatSlot) => {
            if (!staleKeys.has(s.key) && !outranked.has(s.key)) return [s]
            const live = current.get(s.key)
            if (live) return [live]
            // No live row: a stale close's key stays dropped. An outranked key
            // always has one (the write had to find it), so this only ever
            // resolves the stale-close case.
            return staleKeys.has(s.key) ? [] : [s]
          })
          : action.payload
        // A reply in flight can be older than the live frames that arrived while
        // it travelled, so it may omit a slot the stream has since created. The
        // unread drain still runs — that is this path's documented job, and a
        // badge self-heals — but eviction is withheld once the stream is live.
        const fresh = !state.slotsLoaded
        applySlots(state, rows)
        state.slotsGeneration = (state.slotsGeneration ?? 0) + 1
        state.slotsLoaded = true
        reconcileSlots(state, new Set(rows.map((s: { key: string }) => s.key)), fresh)
        settleSlotFetch(state, requestId)
      })
      .addCase(fetchSlots.rejected, (state, action) => {
        settleSlotFetch(state, action.meta?.requestId)
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
          // A same-key recreation supersedes any tombstone (idempotent otherwise).
          if (!isUnsafeKey(action.payload.key)) delete state.closingSlots?.[action.payload.key]
          if (!state.slots.find(s => s.key === action.payload.key)) { // row-read: membership test, adds a row rather than changing one
            state.slots.push(action.payload)
          }
        },
      )
      // Close tombstone lifecycle (see `applySlots`). Matched by type string for
      // the same import-cycle reason as `createSlot` above. `pending` fires
      // BEFORE the thunk body's `removeSlotOptimistic`, so the hold is armed by
      // the time the row leaves the list and no frame can slip between the two.
      .addMatcher(
        (action): action is { type: string; meta: { arg: string; requestId: string } } => action.type === 'chat/deleteSlot/pending',
        (state, action) => {
          // The key is server-minted, but it is still a dynamic property name on
          // a plain record: refuse the prototype keys the way every other
          // per-slot map here does, rather than let Immer throw inside `pending`
          // and leave the DELETE undispatched.
          if (isUnsafeKey(action.meta.arg)) return
          if (!state.closingSlots) state.closingSlots = {}
          // A retry on the same key takes over the hold; the earlier attempt's
          // later lifecycle actions no longer match and are ignored.
          state.closingSlots[action.meta.arg] = {
            requestId: action.meta.requestId,
            inFlightUntil: Date.now() + CLOSE_IN_FLIGHT_MAX_MS,
            graceFrames: CLOSE_CONFIRMED_GRACE_FRAMES,
            awaitingFetches: [],
            confirmedUntil: null,
          }
        },
      )
      .addMatcher(
        (action): action is PayloadAction<string> & { meta?: { requestId?: string } } => action.type === 'chat/deleteSlot/fulfilled',
        (state, action) => {
          // Normally already confirmed by `confirmCloseHold` the moment the
          // DELETE resolved; this covers a fulfilment that never went through
          // the thunk body's dispatch (a test harness, which may also omit meta).
          const requestId = action.meta?.requestId
          if (requestId) confirmHold(state, action.payload, requestId)
        },
      )
      .addMatcher(
        (action): action is { type: string; meta: { arg: string; requestId: string } } => action.type === 'chat/deleteSlot/rejected',
        (state, action) => {
          // Belt and braces: the thunk releases the hold itself before its
          // recovery refetch (see `releaseCloseHold`), because `rejected` only
          // fires after the thunk's trailing `await navigation` and the refetch
          // reply can land first. This covers a rejection that never reached
          // that catch (a thrown `switchSlot` peer selection, a test harness).
          // Owner-checked: this `rejected` can trail a same-key retry's `pending`.
          releaseHold(state, action.meta.arg, action.meta.requestId)
        },
      )
  },
})

export const { sseStatus, sseYolo, setYoloDuration, sseConnected, sseDisconnected, sseSlots, setSidebarOrder, sseTodoUpdate, sseMcpReportUpdate, touchSlotActivity, setChannelTrusted, sseSlotTitle, addSlotOptimistic, removeSlotOptimistic, releaseCloseHold, confirmCloseHold, updateSlot, updateSlotFolder, updateSlotPin, triggerRefresh, markSlotUnread, markSlotRead, remoteSlotRead, setUpdateProgress,
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
 * True when a slot's turns run on a connected crew rather than THIS machine —
 * the single spelling of "crew-bound". Two client surfaces must agree with each
 * other and with the server on it: `selectContinuable` (which must not OFFER
 * Continue on a bound slot) and ChatPage's regenerate / edit-resend gates
 * (which must not offer those either). Mirrors `remote_bound_refusal` in
 * `src/kiro_crew/dashboard/remote_relay.py`, which REFUSES the same actions with
 * 409 `remote_action_unsupported`.
 *
 * Keyed on `executor` (the binding INTENT), never `instance_id` or `is_remote`:
 * the server refuses a half-open binding (marker set, triple incomplete) too, so
 * an unbound `executor` must read as bound here exactly as it does there. An
 * absent slot is not bound — an older gateway ships `executor` on every slot, so
 * only a genuinely missing lookup lands here, and a missing slot has no local
 * action to gate.
 */
export function slotIsRemoteBound(slot: { executor?: string } | null | undefined): boolean {
  return slot?.executor === 'remote'
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
