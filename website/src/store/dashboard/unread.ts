import { safeSetItem, safeSetSessionItem } from '../../utils/safeStorage'
import { newerTs } from '../../lib/slotReadRelay'
import { slotListHoldTest } from '../../utils/slotListHold'
import { crewRootOf } from '../../lib/crewMode'
import type { DashboardState } from '../dashboardSlice'

/** The unread record behind the session badges: its Web Storage persistence,
 *  the count relayed to the Instances hub, and the drain and held-orphan
 *  helpers `dashboardSlice` reconciles slot lists with. `dashboardSlice`
 *  re-exports the names callers import from it. */

/** unreadSince sentinel for a manual mark-as-unread. It parses as an invalid
 *  instant, so the conservative comparison below can never treat any relayed
 *  read watermark as covering it. A bare non-letter char, never rendered —
 *  the constant name carries the meaning. */
export const MANUAL_UNREAD = '\uffff'

/** True when `read` chronologically covers `since`. Timestamps are parsed as
 *  instants — mixed-offset server strings make lexical order lie about time
 *  order — and ANY unparseable side answers false, so an invalid watermark
 *  can never clear a badge (and the manual sentinel never parses). */
export const readCovers = (read: string | undefined, since: string): boolean => {
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
 *  older code read it; nothing in this file does.
 *
 *  Synchronous. Message arrivals go through queueSharedUnread instead; this
 *  call writes any queued arrivals first, so a read or drain that follows an
 *  arrival in this window still lands after it. */
export const persistSharedUnread = (add: Record<string, string>, remove: readonly string[]): void => {
  flushSharedUnread()
  writeSharedUnread(add, remove)
}

/** Arrivals waiting for the next flush: slot -> watermark, merged with the
 *  record's own rule (newest parseable instant wins, '' never demotes). */
let pendingShared: Record<string, string> = {}
let pendingTimer: ReturnType<typeof setTimeout> | null = null
let flushOnHideInstalled = false
/** How long arrivals coalesce before one write. A sibling window sees a new
 *  badge up to this much later; reads and drains in this window flush first,
 *  so they never race a queued arrival. */
export const SHARED_UNREAD_FLUSH_MS = 250

/** Queue a message arrival for the shared record. Called from the
 *  markSlotUnread reducer: background slots re-mark on every arriving row, and
 *  a synchronous read, parse and two writes per row ran on the websocket
 *  dispatch path. One flush per SHARED_UNREAD_FLUSH_MS writes them all. */
export const queueSharedUnread = (slot: string, ts: string): void => {
  if (ts === MANUAL_UNREAD) return  // sentinels never publish
  const prev = pendingShared[slot]
  if (prev === undefined) pendingShared[slot] = ts
  else if (ts !== '') pendingShared[slot] = prev === '' ? ts : (newerTs(prev, ts) ?? prev)
  if (pendingTimer !== null) return
  pendingTimer = setTimeout(flushSharedUnread, SHARED_UNREAD_FLUSH_MS)
  if (!flushOnHideInstalled && typeof window !== 'undefined') {
    // A tab closed inside the coalescing window would otherwise drop the
    // badge for its last arrivals from the record the next boot restores.
    flushOnHideInstalled = true
    window.addEventListener('pagehide', flushSharedUnread)
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushSharedUnread() })
  }
}

/** Drop a slot's queued arrival when a read relayed from another window covers
 *  it. That window already cleared the record for what it read, so writing the
 *  arrival now would bring the badge back for every window. A queued '' (no
 *  watermark) is covered by any read, as it is in the record; a newer queued
 *  watermark survives. */
export const dropQueuedSharedUnreadCoveredBy = (slot: string, readTs: string | undefined): void => {
  const queued = pendingShared[slot]
  if (queued === undefined) return
  if (queued === '' || readCovers(readTs, queued)) delete pendingShared[slot]
}

/** Write queued arrivals now. Exported for tests and for callers that must
 *  observe the record (persistSharedUnread and clearSharedUnreadIfCovered
 *  call it first). */
export const flushSharedUnread = (): void => {
  if (pendingTimer !== null) { clearTimeout(pendingTimer); pendingTimer = null }
  const add = pendingShared
  if (Object.keys(add).length === 0) return
  pendingShared = {}
  writeSharedUnread(add, [])
}

/** Drop queued arrivals without writing them. Test setup calls it before each
 *  test, so one test's queue never lands in the next test's storage. */
export const __resetSharedUnreadQueueForTests = (): void => {
  if (pendingTimer !== null) clearTimeout(pendingTimer)
  pendingTimer = null
  pendingShared = {}
}

const writeSharedUnread = (add: Record<string, string>, remove: readonly string[]): void => {
  try {
    let stored: Record<string, string>
    try { stored = JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string> } catch { stored = {} }
    let changed = false
    for (const k of remove) {
      if (Object.prototype.hasOwnProperty.call(stored, k)) { delete stored[k]; changed = true }
    }
    for (const [k, v] of Object.entries(add)) {
      if (v === MANUAL_UNREAD) continue  // sentinels never publish
      const prev = stored[k]
      if (prev === undefined) { stored[k] = v; changed = true; continue }
      if (v === '') continue  // presence already recorded; never demote a watermark
      const next = prev === '' ? v : (newerTs(prev, v) ?? prev)
      if (next !== prev) { stored[k] = next; changed = true }
    }
    // An arrival the live record already covers (the badge is present and its
    // watermark is at least as new) writes nothing. The read above is of the
    // live record, so a sibling's clear is still seen and the slot's next
    // arrival re-publishes it.
    if (!changed) return
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
export const clearSharedUnreadIfCovered = (slot: string, readTs: string | undefined): string | undefined => {
  // A queued arrival this window has not written yet must be in the record
  // the read below judges.
  flushSharedUnread()
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
export const persistManualSentinels = (unreadSince: Record<string, string>): void => {
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
export const _relayUnreadToParent = (slotsJson: string): void => {
  try {
    if (typeof window === 'undefined' || window.parent === window) return
    const count = (JSON.parse(slotsJson) as string[]).length
    let target = '*'
    try { if (document.referrer) target = new URL(document.referrer).origin } catch { /* keep '*' */ }
    window.parent.postMessage({ source: 'kirocrew', type: 'mc-unread-slots', count }, target)
  } catch { /* never let the relay break a state update */ }
}

/** Relay this window's unread count to the Instances hub, computing it only
 *  when embedded. The count walks every slot row, and inside a reducer each
 *  row read creates an Immer draft, so a standalone window skips the work. */
export const relayUnreadCount = (state: DashboardState): void => {
  try { if (typeof window === 'undefined' || window.parent === window) return } catch { return }
  _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
}

export const drainUnreadKeys = (state: DashboardState, keys: Set<string>): void => {
  const unread = state.unreadSlots ?? []
  const dropped = unread.filter(key => keys.has(key))
  if (dropped.length === 0) return
  if (state.unreadSince) {
    let droppedManual = false
    for (const key of dropped) {
      if (state.unreadSince[key] === MANUAL_UNREAD) droppedManual = true
      delete state.unreadSince[key]
    }
    if (droppedManual) persistManualSentinels(state.unreadSince)
  }
  state.unreadSlots = unread.filter(key => !keys.has(key))
  persistSharedUnread({}, dropped)
  _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
}

/** True for an unread key whose slot is absent and that the next list will not
 *  drain: every orphan while the gateway labels its lists partial, and otherwise
 *  the keys the gateway holds. Counts shown to the user skip these: the entry is
 *  kept for when the tab returns, but no visible session backs it. Before any
 *  labelled list arrives nothing is skipped, so dots restored from storage
 *  still count. */
export const heldOrphanTest = (
  liveKeys: ReadonlySet<string>,
  heldKeys: readonly string[],
  holdChannels: boolean,
  listPartial: boolean,
): ((key: string) => boolean) => {
  const holds = slotListHoldTest({ partial: listPartial, heldKeys, holdChannels })
  return key => !liveKeys.has(key) && holds(key)
}

const statusHeldOrphanTest = (state: DashboardState): ((key: string) => boolean) => heldOrphanTest(
  new Set((state.slots ?? []).map(s => s.key)),
  state.slotsHeldKeys ?? [],
  state.slotsHoldChannels === true,
  state.slotsListPartial === true,
)

/** The keys of the Crew Mode crew members among *slots* that the person's lists
 *  leave out (`crewModeView`): those whose root is a row too, since that root's
 *  Crew panel lists them. Counts shown to the person skip them; their unread
 *  entries stay for that panel. A crew member whose root is closed is a row
 *  again, so it counts. */
export const crewMemberKeys = (slots: readonly { key: string; crew_root?: string }[]): ReadonlySet<string> => {
  const keys = new Set(slots.map(s => s.key))
  return new Set(slots.filter(s => { const root = crewRootOf(s); return !!root && keys.has(root) }).map(s => s.key))
}

/** The unread keys an embedded instance reports to the Instances hub. */
export const relayedUnreadKeys = (state: DashboardState): string[] => {
  const isHeldOrphan = statusHeldOrphanTest(state)
  const crewMembers = crewMemberKeys(state.slots ?? [])
  return (state.unreadSlots ?? []).filter(key => !isHeldOrphan(key) && !crewMembers.has(key))
}

/** True while slot-list state can hide absent unread sessions from the hub count. */
export const hasUnreadRelayHold = (state: DashboardState): boolean =>
  (state.slotsHeldKeys ?? []).length > 0 || state.slotsHoldChannels === true || state.slotsListPartial === true

/** Relay the current hub count only when slot-list hold state can affect it. */
export const relayUnreadIfHolding = (
  state: DashboardState,
  holding: boolean = hasUnreadRelayHold(state),
): void => {
  if (holding) _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
}
