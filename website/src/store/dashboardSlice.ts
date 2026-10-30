import { safeSetItem } from '../utils/safeStorage'
import { newerTs } from '../lib/slotReadRelay'
import { jsonEqual } from '../utils/structuralEqual'
import { slotSurfaceKey } from './slotIdentity'
import { createSlice, createAsyncThunk, createSelector, type PayloadAction } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { streamFlush } from './streamFlush'
import { ApiError } from '../api/apiError'
import { isUnsafeKey } from '../utils/sanitize'
import type { StatusData, ChatSlot, TodoList, McpSessionReport } from '../types'
import { bumpLastTsOverlay, dropCaughtUpLastTs, pruneLastTsOverlay, slotLastTsOf } from './slotLastTs'
import type { SessionColorMode, PaletteName, DefaultColorSetting, IntensityName } from '../utils/sessionColors'
import { isChatPageSurface } from '../utils/channelOrigin'
import { slotListHoldTest } from '../utils/slotListHold'
import { crewModeEnabled } from '../lib/crewMode'
import { MANUAL_UNREAD, _relayUnreadToParent, clearSharedUnreadIfCovered, crewMemberKeys, drainUnreadKeys, dropQueuedSharedUnreadCoveredBy, hasUnreadRelayHold, heldOrphanTest, persistManualSentinels, persistSharedUnread, queueSharedUnread, readCovers, relayUnreadCount, relayUnreadIfHolding, relayedUnreadKeys, restoreUnreadBadges, restoreUnreadSince } from './dashboard/unread'
export { selectPersonSlots } from './personSlots'

/** A close tombstone entry (see `applySlots`). */
interface CloseHold {
  /** The `deleteSlot` attempt that owns this hold. A stale attempt's lifecycle
   *  actions (a `rejected` trailing a slow peer navigation) must not touch a
   *  newer attempt's hold on the same key. */
  requestId: string
  /** Deadline while the DELETE is in flight; `null` once the server has answered. */
  inFlightUntil: number | null
  /** This attempt received 404 because another close popped the key; wait for
   *  that close's durable removal or rollback signal. */
  awaitingOutcome?: true
  /** Straggler lists an awaiting or confirmed hold still survives. The same
   *  counter carries across a durable `removed` outcome. */
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

/** The two user-owned mute flags on a slot. */
export type MuteFlag = 'mutes_opened' | 'muted'
/** The `slotMuteFlagError` key for one flag on one slot. */
export const muteFlagErrorKey = (flag: MuteFlag, slotKey: string) => `${flag}:${slotKey}`

export interface DashboardState {
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
  /** Highest pinned-order revision (`pin_rev`) whose ranks the rows hold. A
   *  frame or answer with a lower one was serialized before a write whose
   *  ranks are already applied, so its ranks are ignored. */
  pinOrderRev?: number
  /** The latest reorder gesture while its write is queued or in flight. It
   *  lives here, not in the sidebar, so a sidebar that remounts mid-write
   *  derives the next gesture from it rather than from an earlier answer. */
  pendingPinOrder?: string[] | null
  /** Why the last pinned-order write failed, shown above the sidebar list
   *  until dismissed or the next reorder; '' when there is nothing to show. */
  pinOrderError?: string
  /** The manual order's counterparts of the three fields above (sort
   *  "Manual"): the highest `manual_rev` applied, the latest gesture's whole
   *  order while its writes are queued, and the last write's failure text. */
  manualOrderRev?: number
  pendingManualOrder?: string[] | null
  manualOrderError?: string
  /** Per-key optimistic/reconciliation pin writes, independent of other slot fields. */
  slotPinGenerations: Record<string, number>
  /** Last failed mute toggle, one record for both user-owned mute flags
   *  (`mutes_opened` and `muted`), keyed by `muteFlagErrorKey(flag, slot)`.
   *  Kept so the menu can render it through ErrorNotice even after the kebab
   *  closed and reopened. Set on the mutation's onError (after the optimistic
   *  flip is rolled back), cleared when a later toggle of that flag starts. */
  slotMuteFlagError: Record<string, string>
  /** Keys whose close is in flight, awaiting another close's outcome, or just
   *  confirmed. An ordinary in-flight key is held out of `slots`; an awaiting
   *  key also survives `CLOSE_CONFIRMED_GRACE_FRAMES` lists before a WebSocket
   *  membership can release it, while a post-404 HTTP reply may release it at
   *  once. Bounded by the wall clock and that shared list budget. */
  closingSlots: Record<string, CloseHold>
  /** Keys this tab saw leave through the server's durable `removed` patch.
   *  History rewrites may trust this immediately; slot-list visibility still
   *  follows `closingSlots` and its straggler budget. */
  durablyRemoved: Record<string, true>
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
  /** Mid-turn `last_ts` bumps held off the row; see `store/slotLastTs`. */
  slotLastTs: Record<string, string>
  /** Per key, the value of `slotWriteSeq` at that row's last MEMBERSHIP write (a
   *  create / resume / fork / companion-open — every `addSlotOptimistic` and
   *  `createSlot.fulfilled`). Compared against a `fetchSlots` request's mark the
   *  same way `slotWrittenAt` is, but it drives the RE-ADD of a key the reply
   *  OMITS, where `slotWrittenAt` drives only the substitution of a key the
   *  reply contains: a content write must never resurrect a row the reply
   *  dropped (that would revive a foreign-deleted chat), while a membership
   *  write the server confirmed must survive a reply serialized before it.
   *  Only a reply dispatched BEFORE the add can re-add the key: `membersOutranking`
   *  re-adds when `slotMemberAt` exceeds the request's mark, and a reply
   *  dispatched after the add carries a mark at or above the stamp, so it never
   *  re-adds — no consumption step is needed. Pruned with the rest of the
   *  per-slot state when an authoritative frame drops the key. */
  slotMemberAt: Record<string, number>
  /** Recent membership adds, by stamped key. A full `slots` frame carries no
   *  ordering mark, so one the gateway serialized before the add can arrive
   *  after it and omit the key; `sseSlots` keeps the row through such a frame
   *  and asks for one `fetchSlots` to settle it (see `retainUnlistedMembers`).
   *  An entry leaves when a `fetchSlots` dispatched after the add omits the
   *  key, when the key is closed or removed, or after `UNLISTED_MEMBER_MAX_MS`. */
  slotMemberUnlisted?: Record<string, UnlistedMember>
  /** Set by `sseSlots` when it kept an unlisted member that still needs a
   *  `fetchSlots` answer, cleared when any `fetchSlots` starts. A reducer
   *  cannot dispatch, so the frame handler sends the read through
   *  `readUnlistedMembers`. */
  slotMemberReadWanted?: boolean
  /** Per in-flight `fetchSlots` requestId, the write sequence and unread-drain
   *  hold state when the request was dispatched. The server serialized its reply
   *  after that instant, so any key whose `slotWrittenAt` is HIGHER was written
   *  locally while the reply travelled and the reply is older than the screen for
   *  that key. A reply started under the unread hold cannot become safe to drain
   *  merely because a complete WebSocket frame arrives before it: the reply may
   *  predate that frame and omit a session it lists. Once such a frame HAS
   *  arrived, its key set (`completeKeys`) is the authority the reply's drain
   *  defers to, so the reply drains only what both lists omit. Entries leave
   *  when the request settles. */
  slotFetchWriteMark: Record<string, {
    writeSeq: number
    unreadDrainHeld: boolean
    heldKeys: string[]
    holdChannels: boolean
    completeKeys: string[] | null
  }>
  // Slot keys in the order the session sidebar actually DISPLAYS them
  // (pinned-first + the user's sort, flat-view aware). Published by
  // ChatSidebar; consumed by the chat-jump / chat-cycle keyboard shortcuts and
  // by deleteSlot's close-landing pick after row identities map to slot keys.
  // Keep the complete displayed order: truncating it to the shortcut count
  // would break adjacent-row close landing. Empty until the sidebar first
  // renders — consumers fall back to `slots` order then.
  sidebarOrder: string[]
  approvalMode: string
  channelTrusted: boolean
  refreshTrigger: number
  /** Bumped when the cron caches are re-read (see `invalidateRefreshQueries`):
   *  what the Schedule page reloads its job list on. */
  cronRefreshTrigger: number
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
  /** While true, `reconcileSlots` keeps every unread entry whose key the slot
   *  list omits instead of draining it. True from boot and from every
   *  disconnect or `sseConnected` until a `slots` frame arrives without
   *  `slotsPartial`: a gateway mid-restart labels the lists it pushes while sessions are still
   *  being restored, and draining against one of those would clear the badge
   *  of a session that merely has not been listed yet. Only the drain is
   *  held; the list itself renders and evicts as usual. */
  unreadDrainHeld: boolean
  /** The last `slots` frame carried `slotsPartial: true`. Unlike
   *  `unreadDrainHeld`, false until the gateway says so: it is what the unread
   *  counts read to leave out sessions a restoring gateway has not listed yet. */
  slotsListPartial: boolean
  /** Known omitted keys that remain unsafe to drain after the global hold clears. */
  slotsHeldKeys: string[]
  /** Hold every channel-origin key when the gateway could not list channel sessions. */
  slotsHoldChannels: boolean
  slotsLoaded: boolean
  /** Every slot-list fetch in flight has failed and no list has landed yet, so
   *  `selectSlotListPending` stops waiting and the pane falls back to its empty
   *  state instead of a placeholder that never resolves. */
  slotsFetchFailed: boolean
  updateProgress: { step: string; detail: string } | null
  // Desktop updater: an update is discoverable/staged (found|downloading|
  // downloaded). Drives the Settings nav dot + the About tab dot. Mirrored
  // from the Electron update-state events by useUpdateSubscription.
  desktopUpdateAvailable: boolean
  sessionDefaultColor: DefaultColorSetting
  sessionColorsMode: SessionColorMode
  sessionColorsPalette: PaletteName
  sessionColorsIntensity: IntensityName
  enabledAppIds: string[]
}

const safeGet = (key: string, fallback: string) => { try { return localStorage.getItem(key) ?? fallback } catch { return fallback } }
export { MANUAL_UNREAD, restoreUnreadBadges, restoreUnreadSince } from './dashboard/unread'
const safeSet = (key: string, value: string) => {
  try { safeSetItem(key, value) } catch { /* QuotaExceededError / SecurityError */ }
  if (key === 'mc-unread-slots') _relayUnreadToParent(value)
}

const initialState: DashboardState = {
  status: null,
  connected: false,
  slots: [],
  slotsGeneration: 0,
  pinOrderRev: 0,
  pendingPinOrder: null,
  pinOrderError: '',
  manualOrderRev: 0,
  pendingManualOrder: null,
  manualOrderError: '',
  slotPinGenerations: {},
  slotMuteFlagError: {},
  closingSlots: {},
  durablyRemoved: {},
  slotFetchesInFlight: [],
  staleSlotFetches: {},
  slotWriteSeq: 0,
  slotWrittenAt: {},
  slotLastTs: {},
  slotMemberAt: {},
  slotFetchWriteMark: {},
  sidebarOrder: [],
  approvalMode: 'normal',
  channelTrusted: false,
  refreshTrigger: 0,
  cronRefreshTrigger: 0,
  ...(() => { const since = restoreUnreadSince(); return { unreadSlots: restoreUnreadBadges(since), unreadSince: since } })(),
  unreadDrainHeld: true,
  slotsListPartial: false,
  slotsHeldKeys: [],
  slotsHoldChannels: false,
  slotsLoaded: false,
  slotsFetchFailed: false,
  updateProgress: null,
  desktopUpdateAvailable: false,
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
  async ({ mode, slot }, { rejectWithValue, getState }) => {
    try {
      // YOLO is one app-wide setting, and the gateway keeps it on when ONE slot
      // asks for `trust` / `trust_reads` (an automation setting up a new session
      // must not end the operator's YOLO). So a person moving this chat from
      // YOLO to Trust or Reads would see YOLO come back on the next status
      // frame. Send `normal` first: it is the documented way to turn YOLO off,
      // and the chosen mode is then applied to this slot alone.
      const current = (getState() as { dashboard?: DashboardState }).dashboard?.approvalMode
      if (current === 'yolo' && slot && (mode === 'trust' || mode === 'trust_reads')) {
        await api.chatMode('normal', slot)
      }
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

/** Reconcile per-slot dashboard state against an authoritative slot list. Both
 *  authoritative writers (`sseSlots`, `fetchSlots.fulfilled`) drive teardown
 *  through here, so the two cannot drift apart the way the eviction lists this
 *  PR unified once did. `unreadSlots` is written back only when it actually
 *  shrank, since the live-frame writer runs on every slots frame. */
const reconcileSlots = (
  state: DashboardState,
  liveKeys: Set<string>,
  evictStale = true,
  unreadDrainHeld = state.unreadDrainHeld,
  heldKeys = state.slotsHeldKeys,
  holdChannels = state.slotsHoldChannels,
): void => {
  // `countUnreadByMode` deliberately keeps orphan unread keys contributing to
  // the badge, on the premise that a reconcile drains them shortly. Draining on
  // both writers is what keeps that premise true. Always run: a wrongly drained
  // badge self-heals on the next unread event, and the refetch is the documented
  // route by which a remotely deleted slot's badge is cleared.
  //
  // The one exception is a list the gateway labelled partial (`unreadDrainHeld`):
  // a session it has not restored yet is absent from that list without having
  // been deleted, and a drain here would be the lossy direction. The hold covers
  // a GET reply too, since it goes through this same path. Deferring the drain
  // is safe: the next complete list drains whatever is genuinely gone.
  const holds = slotListHoldTest({ partial: unreadDrainHeld, heldKeys: heldKeys ?? [], holdChannels })
  drainUnreadKeys(state, new Set((state.unreadSlots ?? []).filter(key => !liveKeys.has(key) && !holds(key))))
  // Eviction is NOT recoverable, so it is skipped when the caller cannot vouch
  // for the list's freshness: an HTTP reply in flight can be older than the live
  // frames that arrived while it travelled, and would then delete a slot the
  // stream has since created.
  if (!evictStale) return
  // A write stamp for a key the authoritative list has
  // dropped protects nothing, and keeping it would let the record grow for the
  // tab's lifetime. Pruned only when the caller vouches for the list, so an HTTP
  // reply that merely predates a newly created slot cannot strip its protection.
  for (const stamped of Object.keys(state.slotWrittenAt ?? {})) {
    if (!liveKeys.has(slotKeyOfStamp(stamped))) delete state.slotWrittenAt[stamped]
  }
  for (const stamped of Object.keys(state.slotMemberAt ?? {})) {
    if (!liveKeys.has(slotKeyOfStamp(stamped))) delete state.slotMemberAt[stamped]
  }
  pruneLastTsOverlay(state, liveKeys)
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
 *  row. One narrow exception follows a 404 DELETE: that attempt knows another
 *  close popped the key and waits for the other close's outcome. An unpaired
 *  WebSocket list can still predate the pop, so the awaiting hold first spends
 *  `CLOSE_CONFIRMED_GRACE_FRAMES`; after that, membership releases it. A
 *  post-404 HTTP reply may release it immediately because every fetch already
 *  in flight at the 404 is paired and filtered first. A durable `removed` patch
 *  confirms the hold instead. An awaiting hold that sees neither `removed` nor
 *  a list including the key expires on `CLOSE_IN_FLIGHT_MAX_MS` (evaluated by
 *  the next list, and by a timer `deleteSlot` arms so an idle tab that receives
 *  no list still reaches it), and the key's entry then degrades to `main`'s
 *  behaviour: the hold is dropped, the next list applies membership, and the
 *  URL writer pushes over the entry rather than rewriting it.
 *
 *  Every other tombstone is retired by the close's OWN lifecycle: while
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
  if (!hold || hold.requestId !== requestId || hold.inFlightUntil === null || hold.awaitingOutcome) return
  hold.inFlightUntil = null
  hold.awaitingFetches = [...(state.slotFetchesInFlight ?? [])]
  hold.confirmedUntil = Date.now() + CLOSE_CONFIRMED_MAX_MS
}
/** Arm a tombstone directly in its confirmed phase for a removal the server
 *  already acknowledged. Pre-removal fetches remain paired with the hold so
 *  their replies cannot restore the closed row. */
const armConfirmedHold = (state: DashboardState, key: string): void => {
  if (isUnsafeKey(key)) return
  if (!state.closingSlots) state.closingSlots = {}
  state.closingSlots[key] = {
    requestId: 'confirmed',
    inFlightUntil: null,
    graceFrames: CLOSE_CONFIRMED_GRACE_FRAMES,
    awaitingFetches: [...(state.slotFetchesInFlight ?? [])],
    confirmedUntil: Date.now() + CLOSE_CONFIRMED_MAX_MS,
  }
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
/** The `slotWriteSeq` value of `slotKey`'s last single-slot write, or 0. A
 *  caller compares it with `slotWriteSeq` read earlier to learn whether some
 *  writer touched the row since. */
export const slotWriteStampOf = (state: DashboardState, slotKey: string): number =>
  state.slotWrittenAt?.[stampKey(slotKey)] ?? 0

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

/** Record that `key` was just ADDED to membership (a create / resume / fork /
 *  companion-open). Bumps the counter and stamps ONLY `slotMemberAt`, so a
 *  reply that OMITS the key re-adds it (issue #14831) rather than dropping it.
 *
 *  Deliberately NOT a content stamp: membership says the key EXISTS, not that
 *  this tab's optimistic row is newer than the server's. A reply that genuinely
 *  LISTS the key carries the full server row (`mode`/`surface`/`color_index`/
 *  `project`/…), and stamping `slotWrittenAt` here would send that reply down
 *  the substitution branch and keep the thinner optimistic row instead. The
 *  create site adds its own content stamp, because its payload alone carries
 *  locally-applied fields the server has not confirmed. */
const stampSlotMember = (state: DashboardState, key: string): void => {
  state.slotWriteSeq = (state.slotWriteSeq ?? 0) + 1
  if (!state.slotMemberAt) state.slotMemberAt = {}
  state.slotMemberAt[stampKey(key)] = state.slotWriteSeq
  if (!state.slotMemberUnlisted) state.slotMemberUnlisted = {}
  state.slotMemberUnlisted[stampKey(key)] = { seq: state.slotWriteSeq, at: Date.now(), verifying: false }
}

/** An entry of `slotMemberUnlisted`. `verifying` is set once a `fetchSlots`
 *  has been asked for, so a run of stale frames sends one request, not one
 *  per frame. */
export interface UnlistedMember { seq: number; at: number; verifying: boolean }

/** How long a membership add may outlive full frames that omit it while no
 *  `fetchSlots` answer arrives (the request failed or never settled). Past it,
 *  the frame wins, as it did before the add was tracked. */
const UNLISTED_MEMBER_MAX_MS = 15_000

/** The rows a full `slots` frame should be applied as, given the membership
 *  adds no frame has listed yet.
 *
 *  A frame the gateway serialized before a create or fork can reach the tab
 *  after the op's HTTP response. It omits the new key, and applying it as a
 *  whole-list replace would drop the row the tab just opened; the chat page
 *  then clears the active session and reopens the remembered one. So a key
 *  still in `slotMemberUnlisted` is kept, with its live row appended after the
 *  frame's rows, and a `fetchSlots` is requested. That fetch is dispatched
 *  after the add, so its reply is serialized after the server registered the
 *  key: a reply that omits it means the chat was deleted elsewhere, and ends
 *  the entry (`settleUnlistedMembers`). Such a chat is shown for at most one
 *  GET round trip after the frame that omitted it.
 *
 *  A frame or reply that lists the key does not end the entry: an older
 *  frame can still be in flight behind it, on the socket or against the HTTP
 *  reply. The entry runs out on its time cap instead. */
const retainUnlistedMembers = (state: DashboardState, rows: ChatSlot[]): ChatSlot[] => {
  const unlisted = state.slotMemberUnlisted
  if (!unlisted) return rows
  const stampedKeys = Object.keys(unlisted)
  if (!stampedKeys.length) return rows
  const listed = new Set(rows.map(s => s.key))
  const now = Date.now()
  let kept: ChatSlot[] | null = null
  for (const stamped of stampedKeys) {
    const key = slotKeyOfStamp(stamped)
    const entry = unlisted[stamped]
    const live = (state.slots ?? []).find(s => s.key === key) // row-read: membership carry-over, the live row is kept unchanged
    if (!live || state.closingSlots?.[key] || now - entry.at > UNLISTED_MEMBER_MAX_MS) {
      delete unlisted[stamped]
      continue
    }
    if (listed.has(key)) continue
    if (!kept) kept = [...rows]
    kept.push(live)
    if (!entry.verifying) {
      entry.verifying = true
      state.slotMemberReadWanted = true
    }
  }
  return kept ?? rows
}

/** A `fetchSlots` request settled. Its reply was serialized after every add
 *  stamped at or below its mark, so for those keys it is the answer
 *  `retainUnlistedMembers` waited for: their entries end, and the reply's own
 *  list decides whether the row stays. A failed request (`listed` null)
 *  answers nothing, so it only re-arms the request for the next omitting
 *  frame. Runs before `settleSlotFetch`, which drops the mark. */
const settleUnlistedMembers = (state: DashboardState, requestId: string | undefined, listed: Set<string> | null): void => {
  const unlisted = state.slotMemberUnlisted
  const mark = requestId === undefined ? undefined : state.slotFetchWriteMark?.[requestId]?.writeSeq
  if (!unlisted || mark === undefined) return
  for (const stamped of Object.keys(unlisted)) {
    if (unlisted[stamped].seq > mark) continue
    // A failed read, or one that lists the key, settles nothing about frames
    // still in flight on the socket: the entry stays, and the next frame that
    // omits the key asks again.
    if (!listed || listed.has(slotKeyOfStamp(stamped))) { unlisted[stamped].verifying = false; continue }
    delete unlisted[stamped]
    // A reply newer than the add that omits the key is the server saying it
    // is gone. Drop the membership stamp too, so a reply sent before the add
    // and still in flight cannot re-add it through `membersOutranking`.
    delete state.slotMemberAt?.[stamped]
  }
}

/** Whether a full frame omits a key `slotMemberUnlisted` still tracks. The
 *  frame handler must then run `sseSlots` even for a byte-identical repeat:
 *  that run is what applies the time cap and, after a failed read, asks for
 *  the read again. Skipping it would keep the row with no end. */
export const omitsUnlistedMember = (dashboard: DashboardState, rows: unknown): boolean => {
  const unlisted = dashboard.slotMemberUnlisted
  if (!unlisted || !Array.isArray(rows)) return false
  const stamped = Object.keys(unlisted)
  if (!stamped.length) return false
  const listed = new Set((rows as ChatSlot[]).map(s => s.key))
  return stamped.some(k => !listed.has(slotKeyOfStamp(k)))
}

/** Send the `fetchSlots` that `sseSlots` asked for when it kept a session a
 *  stale frame omitted (see `retainUnlistedMembers`). Dispatched by the frame
 *  handler right after `sseSlots`. */
export const readUnlistedMembers = () =>
  (dispatch: (action: ReturnType<typeof fetchSlots>) => unknown, getState: () => { dashboard: DashboardState }): void => {
    if (getState().dashboard.slotMemberReadWanted) dispatch(fetchSlots())
  }

/** A single-slot mutation. Returning `false` means the writer's own guard
 *  declined and the row was left alone, so no write is stamped. */
type RowPatch = (slot: ChatSlot) => boolean | void

/** Payload of the `slot_patch` WebSocket frame (see `sseSlotPatch`). */
export interface SlotPatchFrame {
  /** Partial rows: `key` plus the fields that changed. */
  slots?: Array<Partial<ChatSlot> & { key: string }>
  /** Keys that left the server's registry. */
  removed?: string[]
}

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
 *  This guards row CONTENT. A membership ADD (`addSlotOptimistic`,
 *  `createSlot.fulfilled`) carries its own narrower stamp through
 *  `stampSlotMember`, which is what lets a `fetchSlots` reply RE-ADD a key it
 *  omits; a content stamp from here never does, so a rename cannot resurrect a
 *  row another client deleted mid-flight. A removal (`removeSlotOptimistic`) is
 *  reconciled by the close-tombstone machinery and by `applySlots`, which are
 *  already ordered against each other, and so needs no stamp here. */
/** A slot row's value for `field`, as a `slot_patch` row would state it. A full
 *  frame omits `lineage_pending` when it is false, while a patch states it either
 *  way, because a patch that omitted it could never clear a pending row. So an
 *  absent flag reads as `false` here, and a patch saying `false` to a row that
 *  never carried the flag is no write at all. */
const slotPatchCurrent = (slot: Record<string, unknown>, field: string): unknown =>
  field === 'lineage_pending' ? (slot[field] ?? false) : slot[field]

const patchSlotRow = (state: DashboardState, key: string, patch: RowPatch): void => {
  const slot = (state.slots ?? []).find(s => s.key === key) // row-write: via patchSlotRow
  if (!slot) return
  if (patch(slot) === false) return
  stampSlotWrite(state, key)
}

/** The `touchSlotActivity` write, shared with the per-frame `streamFlush`.
 *  An unsettled bump on a row that has `last_turn_ts` goes to the
 *  `slotLastTs` overlay instead of the row (see `store/slotLastTs`). */
const applySlotActivity = (state: DashboardState, { key, ts, settled }: { key: string; ts: string; settled?: boolean }): void => {
  const row = state.slots.find(s => s.key === key) // row-read: picks the overlay or a row write
  if (row && !settled && row.last_turn_ts) {
    // Stamped like a row write, so an older fetchSlots reply cannot undo it.
    if (bumpLastTsOverlay(state, key, ts)) stampSlotWrite(state, key)
    return
  }
  patchSlotRow(state, key, slot => {
    const t = Date.parse(ts)
    let moved = false
    const lastTsMoved = !slot.last_ts || Date.parse(slot.last_ts) <= t // last-ts: the row writer
    if (lastTsMoved) { slot.last_ts = ts; moved = true } // last-ts: the row writer
    if (settled && (!slot.last_turn_ts || Date.parse(slot.last_turn_ts) <= t)) { slot.last_turn_ts = ts; moved = true }
    // Advance last_activity_ts only for settled events (user-sent prompts):
    // unsettled bumps are streamed tool calls on a running session, and
    // advancing on every one would re-rank running sessions mid-turn in the
    // session grid — the same problem that last_turn_ts avoids.
    if (settled && lastTsMoved) {
      const prevActivity = slot.last_activity_ts ? Date.parse(slot.last_activity_ts) : NaN
      if (!slot.last_activity_ts || !Number.isFinite(prevActivity) || t > prevActivity) {
        slot.last_activity_ts = ts
      }
    }
    // A bump both guards rejected changed nothing, so it is not a write and
    // must not outrank a reply: stamping it would withhold a server row on
    // the strength of a no-op.
    return moved
  })
  if (row) dropCaughtUpLastTs(state, [row])
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
  const mark = state.slotFetchWriteMark?.[requestId]?.writeSeq
  if (mark === undefined) return outranked
  const writtenAt = state.slotWrittenAt ?? {}
  for (const stamped of Object.keys(writtenAt)) {
    if (writtenAt[stamped] > mark) outranked.add(slotKeyOfStamp(stamped))
  }
  return outranked
}

/** Keys MEMBERSHIP-added after `requestId` was dispatched, read from
 *  `slotMemberAt` the same way `localWritesOutranking` reads `slotWrittenAt`.
 *  These are the only keys a `fetchSlots` reply may RE-ADD when it omits them:
 *  the add is a create / resume / fork / companion-open the server confirmed,
 *  so a reply serialized before it is simply too old to list it. A content
 *  write stamps `slotWrittenAt` but NOT this record, so it can keep its own
 *  on-screen row (substitution) yet can never resurrect a key the reply
 *  dropped — the row may have been deleted by another client in the same window
 *  (GPT F1).
 *
 *  A reply dispatched AFTER the add carries a mark at or above the stamp, so
 *  `memberAt > mark` is false and it cannot re-add the key: the re-add is bounded
 *  to replies that predate the add, with no explicit consumption step. */
const membersOutranking = (state: DashboardState, requestId: string | undefined): Set<string> => {
  const added = new Set<string>()
  if (requestId === undefined) return added
  const mark = state.slotFetchWriteMark?.[requestId]?.writeSeq
  if (mark === undefined) return added
  const memberAt = state.slotMemberAt ?? {}
  for (const stamped of Object.keys(memberAt)) {
    if (memberAt[stamped] > mark) added.add(slotKeyOfStamp(stamped))
  }
  return added
}

/** The gateway orders a slot row carries a place in: the place field, the
 *  revision field beside it, and the state field holding the highest revision
 *  whose places the rows hold. */
const ORDER_GATES = [
  { rank: 'pin_rank', rev: 'pin_rev', applied: 'pinOrderRev' },
  { rank: 'manual_rank', rev: 'manual_rev', applied: 'manualOrderRev' },
] as const
type OrderGate = typeof ORDER_GATES[number]

/** *rows* with one order's revision folded into its state field. The revision
 *  is not kept on the rows (every row would change on every reorder). Rows
 *  from a list older than the places already applied keep the places they
 *  have. A list with no revision (a scoped stream) passes as is. */
const gateOrder = (state: DashboardState, rows: ChatSlot[], gate: OrderGate): ChatSlot[] => {
  let rev = -1
  for (const row of rows) {
    const value = row[gate.rev]
    if (typeof value === 'number' && value > rev) rev = value
  }
  if (rev < 0) return rows
  const stale = rev < (state[gate.applied] ?? 0)
  if (!stale) state[gate.applied] = rev
  const current = stale ? new Map((state.slots ?? []).map(s => [s.key, s])) : null
  return rows.map(row => {
    const { [gate.rev]: _rev, ...rest } = row
    const existing = current?.get(row.key)
    return existing ? { ...rest, [gate.rank]: existing[gate.rank] ?? null } : rest
  })
}

/** Paint one order onto the rows. A bare list is the person's own gesture,
 *  shown before the gateway answers; `{order, rev}` is the gateway's answer,
 *  applied only if no newer revision of that order has landed since. A row
 *  `placed` rejects holds no place in it. */
const paintOrder = (
  state: DashboardState,
  payload: string[] | { order: string[]; rev: number },
  gate: OrderGate,
  placed: (slot: ChatSlot) => boolean,
): void => {
  let order: string[]
  if (Array.isArray(payload)) order = payload
  else {
    if (payload.rev < (state[gate.applied] ?? 0)) return
    state[gate.applied] = payload.rev
    order = payload.order
  }
  const rank = new Map(order.map((key, index) => [key, index]))
  patchSlotRowsWhere(state, slot => {
    const next = placed(slot) ? (rank.get(slot.key) ?? null) : null
    if ((slot[gate.rank] ?? null) === next) return false
    slot[gate.rank] = next
  })
}

/** *rows* gated by every order's revision (see `gateOrder`). */
const gatePinOrder = (state: DashboardState, rows: ChatSlot[]): ChatSlot[] =>
  ORDER_GATES.reduce((gated, gate) => gateOrder(state, gated, gate), rows)

const applySlots = (state: DashboardState, rawRows: ChatSlot[]): void => {
  const incomingRows = gatePinOrder(state, rawRows)
  // `durablyRemoved` is the durable fast path for browser-history rewrites: a
  // full list naming the key proves it is live again and clears that evidence.
  // This does not change slot-list visibility; the hold and frame budget below
  // still decide whether the named row is shown in this application.
  for (const { key } of incomingRows) {
    if (!isUnsafeKey(key)) delete state.durablyRemoved?.[key]
  }
  let next = incomingRows
  const closing = state.closingSlots ?? {}
  const closingKeys = Object.keys(closing)
  if (closingKeys.length) {
    const held = new Set<string>()
    const now = Date.now()
    for (const key of closingKeys) {
      const hold = closing[key]
      if (hold.inFlightUntil !== null) {
        if (now < hold.inFlightUntil) {
          if (hold.awaitingOutcome) {
            if (hold.graceFrames > 0) {
              hold.graceFrames -= 1
              held.add(key)
              continue
            }
            if (incomingRows.some(s => s.key === key)) {
              // The straggler budget is spent, so membership now means the
              // competing close restored the slot or a same-key session exists.
              delete closing[key]
              continue
            }
          }
          held.add(key)
          continue
        }
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
  if (changed) {
    state.slots = merged
    // A listed slot can turn a held orphan back into a counted session.
    relayUnreadIfHolding(state)
  }
  dropCaughtUpLastTs(state, merged)
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
      // Every frame carries a fresh `disabled_approval_modes` array; keep the
      // stored one when equal so its selector (one per composer) stays put.
      if (prev?.disabled_approval_modes && jsonEqual(prev.disabled_approval_modes, next.disabled_approval_modes)) {
        next.disabled_approval_modes = prev.disabled_approval_modes
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
    sseConnected(state) {
      const hadUnreadRelayHold = hasUnreadRelayHold(state)
      state.connected = true
      state.slotsLoaded = false
      state.unreadDrainHeld = true
      state.slotsListPartial = false
      state.slotsHeldKeys = []
      state.slotsHoldChannels = false
      relayUnreadIfHolding(state, hadUnreadRelayHold)
    },
    sseDisconnected(state) { state.connected = false; state.unreadDrainHeld = true },
    /** The `slotsPartial` label on a `slots` frame (see `unreadDrainHeld`).
     *  Dispatched for every frame, before its list: an absent field is an
     *  older gateway and reads as complete. */
    sseSlotsPartial(state, action: PayloadAction<boolean>) {
      state.unreadDrainHeld = action.payload
      if (state.slotsListPartial === action.payload) return
      state.slotsListPartial = action.payload
      _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
    },
    sseSlotListHolds(
      state,
      action: PayloadAction<{ heldKeys: string[]; holdChannels: boolean }>,
    ) {
      // Assigned only on a real change: this runs for every slots frame, and a
      // fresh array each time would break the unread-count selectors' memo.
      const { heldKeys, holdChannels } = action.payload
      const current = state.slotsHeldKeys ?? []
      const keysChanged = heldKeys.length !== current.length || heldKeys.some((key, i) => key !== current[i])
      if (!keysChanged && holdChannels === state.slotsHoldChannels) return
      if (keysChanged) state.slotsHeldKeys = heldKeys
      state.slotsHoldChannels = holdChannels
      _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
    },
    sseSlots(state, action: PayloadAction<ChatSlot[]>) {
      // A frame the gateway labelled complete (`sseSlotsPartial` ran first) is
      // the authority every GET reply still in flight defers to for its unread
      // drain, so its key set is recorded on each of them. Recorded before the
      // empty-frame return below: a complete empty list is exactly the case
      // where the reply must drain, and that return applies nothing itself.
      // A frame the socket drops as an exact repeat never reaches here, so the
      // mark keeps its hold until the next differing frame; that errs toward
      // keeping a dot.
      if (!state.unreadDrainHeld) {
        for (const mark of Object.values(state.slotFetchWriteMark ?? {})) {
          mark.completeKeys = action.payload.map(s => s.key)
        }
      }
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
      const rows = retainUnlistedMembers(state, action.payload)
      applySlots(state, rows)
      state.slotsGeneration = (state.slotsGeneration ?? 0) + 1
      state.slotsLoaded = true
      reconcileSlots(state, new Set(rows.map(s => s.key)))
    },
    // Sidebar → shortcut and close-landing order feed (see
    // DashboardState.sidebarOrder). The dispatch site diff-guards, so every
    // action here is a real order change.
    setSidebarOrder(state, action: PayloadAction<string[]>) { state.sidebarOrder = action.payload },
    // Live TODO-list delta. Patched into the SAME slots array that sseSlots
    // populates rather than a parallel map, so the mid-turn push and the
    // reconnect snapshot can never disagree about a slot's list. A delta for an
    // unknown slot is dropped — the next sseSlots push carries it anyway.
    sseTodoUpdate(state, action: PayloadAction<{ slot: string; todo: TodoList | null }>) {
      patchSlotRow(state, action.payload.slot, slot => {
        if (!jsonEqual(slot.todo, action.payload.todo)) slot.todo = action.payload.todo
      })
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
      patchSlotRow(state, action.payload.slot, slot => {
        if (!jsonEqual(slot.mcp_report, action.payload.mcp_report)) slot.mcp_report = action.payload.mcp_report
      })
    },
    // Bump a slot's recency timestamps on live message activity so the sidebar
    // re-ranks immediately off the finer-grained chat_message stream (vs waiting
    // for the next full sseSlots push). `last_ts` is the last saved message of any role,
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
      applySlotActivity(state, action.payload)
    },
    setChannelTrusted(state, action: PayloadAction<boolean>) { state.channelTrusted = action.payload },
    sseSlotTitle(state, action: PayloadAction<{ key: string; title: string }>) {
      patchSlotRow(state, action.payload.key, slot => { slot.title = action.payload.title })
    },
    /** A `slot_patch` frame: the server's one-row answer to a metadata edit
     *  (pin, rename, folder move) or a close, sent INSTEAD of the full slot list
     *  to a socket that declared the capability (see `useWebSocket`).
     *
     *  Each `slots` row carries `key` plus only the fields that changed, merged
     *  through `patchSlotRow` so a `fetchSlots` reply already in flight cannot
     *  put the older value back. A row for a key this tab does not hold is
     *  dropped here; the frame handler refetches the list for it.
     *
     *  `removed` keys go through `applySlots` as the current list minus those
     *  keys, the same path a full list takes: close tombstones spend their
     *  frame budget, untouched rows keep their identity, and `reconcileSlots`
     *  tears down the departed key's sub-agent and unread state. The fetches in
     *  flight at that moment are marked stale for the removed key, so their
     *  replies cannot restore it, while a later WebSocket list that names the
     *  key again (a same-key replacement) applies as usual. `slotsGeneration` is
     *  left alone because this is not a full snapshot, and the pin reconciler
     *  counts only those. */
    sseSlotPatch(state, action: PayloadAction<SlotPatchFrame>) {
      const { slots: rows, removed } = action.payload
      for (const row of rows ?? []) {
        if (!row || typeof row.key !== 'string') continue
        const { key, ...fields } = row as typeof row & { pin_rev?: number; manual_rev?: number }
        for (const gate of ORDER_GATES) {
          const rev = (fields as Record<string, unknown>)[gate.rev]
          if (typeof rev !== 'number') continue
          delete (fields as Record<string, unknown>)[gate.rev]
          // One row's patch: a rank serialized before an applied write is
          // dropped; a newer one moves the revision for the full list to follow.
          if (rev < (state[gate.applied] ?? 0)) delete (fields as Record<string, unknown>)[gate.rank]
          else state[gate.applied] = rev
        }
        patchSlotRow(state, key, slot => {
          if (Object.keys(fields).every(field => jsonEqual(
            slotPatchCurrent(slot as unknown as Record<string, unknown>, field),
            (fields as Record<string, unknown>)[field],
          ))) return false
          Object.assign(slot, fields)
        })
      }
      const gone = new Set((removed ?? []).filter(key => !isUnsafeKey(key)))
      // A removal names a slot the server has durably deleted. Unlike a key
      // merely absent from a partial list, it is safe to drain during restart
      // and before the first complete snapshot.
      drainUnreadKeys(state, gone)
      if (gone.size && state.slotsLoaded) {
        for (const key of gone) {
          if (!isUnsafeKey(key) && !state.closingSlots?.[key]) {
            markStaleSlotFetches(state, key, [...(state.slotFetchesInFlight ?? [])])
          }
        }
        const remaining = (state.slots ?? []).filter(s => !gone.has(s.key)) // row-read: membership filter, rows are carried over unchanged
        applySlots(state, remaining)
        // `remaining` is the local list, which `removeSlotOptimistic` already
        // holds every in-flight close out of, so it under-states what is live:
        // a key whose own DELETE has not been answered is still a session on
        // the server, and this frame says nothing about it. The teardown is not
        // recoverable (see `reconcileSlots`), so every tombstoned key this frame
        // did not remove counts as live here; only the keys named in `removed`
        // are torn down.
        const live = new Set(remaining.map(s => s.key))
        for (const key of Object.keys(state.closingSlots ?? {})) if (!gone.has(key)) live.add(key)
        reconcileSlots(state, live)
      }
      // `removed` is durable, so record it even before the first reconnect
      // snapshot and confirm any awaiting hold. History may trust this at once;
      // the hold still spends its list budget before the row can reappear.
      for (const key of removed ?? []) {
        // A server-confirmed removal ends any retention of the key.
        delete state.slotMemberUnlisted?.[stampKey(key)]
        if (isUnsafeKey(key)) continue
        if (!state.durablyRemoved) state.durablyRemoved = {}
        state.durablyRemoved[key] = true
        const hold = state.closingSlots?.[key]
        if (!hold || hold.inFlightUntil === null) continue
        hold.inFlightUntil = null
        hold.awaitingFetches = [...(state.slotFetchesInFlight ?? [])]
        hold.confirmedUntil = Date.now() + CLOSE_CONFIRMED_MAX_MS
        delete hold.awaitingOutcome
      }
    },
    addSlotOptimistic(state, action: PayloadAction<ChatSlot>) {
      // A resume or fork under a key that was closing supersedes the tombstone:
      // the caller has a fresh server acknowledgement that the key is live.
      if (!isUnsafeKey(action.payload.key)) {
        delete state.closingSlots?.[action.payload.key]
        delete state.durablyRemoved?.[action.payload.key]
      }
      if (!state.slots.find(s => s.key === action.payload.key)) { // row-read: membership test, adds a row rather than changing one
        state.slots.push(action.payload)
        relayUnreadIfHolding(state)
      }
      // Every add site routes through this reducer (resume, fork, companion
      // chat, Papyrus), so stamping membership here covers them all by
      // construction: a `fetchSlots` serialized before the add omits the key,
      // and the stamp lets `membersOutranking` re-add the row the whole-list
      // replace would drop (issue #14831). Stamped even when the row is already
      // present — the add still asserts the key is current as of now.
      stampSlotMember(state, action.payload.key)
    },
    /** Drop a close tombstone (see `applySlots`). `deleteSlot` dispatches this in
     *  its failure path BEFORE the recovery `fetchSlots`, because the thunk's
     *  `rejected` action only fires after a trailing `await navigation` (a peer
     *  transcript load, unbounded), and the refetch reply must not be filtered
     *  by the very hold the failed close armed.
     *
     *  `distrustInFlight` is for a DELETE answered 404: the server had already
     *  popped the key, so a `fetchSlots` still in flight at that answer may
     *  have been serialized before the pop, and with the hold gone nothing else
     *  stops its reply re-adding the row. Those requests are paired with the key
     *  in `staleSlotFetches`, as the `removed` branch of `sseSlotPatch` does,
     *  before the release; the recovery refetch dispatched after it is not. */
    releaseCloseHold(state, action: PayloadAction<{ key: string; requestId: string; distrustInFlight?: boolean }>) {
      const { key, requestId, distrustInFlight } = action.payload
      if (distrustInFlight && !isUnsafeKey(key)) markStaleSlotFetches(state, key, [...(state.slotFetchesInFlight ?? [])])
      releaseHold(state, key, requestId)
    },
    /** This attempt's DELETE received 404 after another close popped the key.
     *  Distrust lists already in flight, then wait for that close's outcome. */
    awaitCloseOutcome(state, action: PayloadAction<{ key: string; requestId: string }>) {
      const { key, requestId } = action.payload
      const hold = isUnsafeKey(key) ? undefined : state.closingSlots?.[key]
      if (!hold || hold.requestId !== requestId || hold.inFlightUntil === null) return
      hold.awaitingOutcome = true
      markStaleSlotFetches(state, key, [...(state.slotFetchesInFlight ?? [])])
    },
    /** The wall-clock cap on an awaiting hold, reached without a list. `applySlots`
     *  evaluates `inFlightUntil` only when an authoritative list arrives, and an
     *  idle main-dashboard tab may receive none, so `deleteSlot` arms a timer for
     *  the deadline that dispatches this. Drops the hold exactly as the stalled
     *  branch of `applySlots` would; a confirmed, released, retried or already
     *  expired hold is left alone, so a late or redundant fire is a no-op. */
    expireCloseHold(state, action: PayloadAction<{ key: string; requestId: string }>) {
      const { key, requestId } = action.payload
      const hold = isUnsafeKey(key) ? undefined : state.closingSlots?.[key]
      if (!hold || hold.requestId !== requestId || !hold.awaitingOutcome || hold.inFlightUntil === null) return
      if (Date.now() < hold.inFlightUntil) return
      delete state.closingSlots[key]
    },
    /** The DELETE resolved: the server has popped the slot. Dispatched by
     *  `deleteSlot` before it awaits the peer navigation (see `confirmHold`). */
    confirmCloseHold(state, action: PayloadAction<{ key: string; requestId: string }>) {
      confirmHold(state, action.payload.key, action.payload.requestId)
    },
    /** A removal the server already confirmed (no DELETE of ours in flight): arm
     *  the tombstone straight in its confirmed phase so a pre-pop straggler list cannot re-add the row. */
    armConfirmedCloseHold(state, action: PayloadAction<string>) {
      armConfirmedHold(state, action.payload)
    },
    removeSlotOptimistic(state, action: PayloadAction<string>) {
      state.slots = state.slots.filter(s => s.key !== action.payload)
      delete state.slotMemberUnlisted?.[stampKey(action.payload)]
      state.unreadSlots = state.unreadSlots.filter(k => k !== action.payload)
      if (state.unreadSince?.[action.payload] !== undefined) {
        const wasManual = state.unreadSince[action.payload] === MANUAL_UNREAD
        delete state.unreadSince[action.payload]
        if (wasManual) persistManualSentinels(state.unreadSince)
      }
      persistSharedUnread({}, [action.payload])
      _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
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
      action: PayloadAction<{ url: string; state?: NonNullable<ChatSlot['source_links']>[number]['state']; ci?: NonNullable<ChatSlot['source_links']>[number]['ci']; ciPending?: string }>,
    ) {
      const { url } = action.payload
      if (!url) return
      patchSlotRowsWhere(state, slot => {
        if (!slot.source_links) return false
        let touched = false
        for (const link of slot.source_links) {
          if (link.url !== url) continue
          if (action.payload.state !== undefined) { link.state = action.payload.state; touched = true }
          if (action.payload.ci !== undefined) {
            link.ci = action.payload.ci
            // The marker describes the same rollup as `ci`, so a delta that
            // carries `ci` without it means nothing is still running.
            if (action.payload.ciPending) link.ciPending = action.payload.ciPending
            else delete link.ciPending
            touched = true
          }
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
    /**
     * Drop the link rows that describe ONE binding from a slot, in place. The
     * write counterpart of `patchSlotLink` for an unlink: the binding is gone
     * server-side, so the rows that described it go too, and nothing else in
     * `links` is rebuilt (a whole-array rewrite from a captured snapshot is what
     * made two concurrent toggles unsafe). An `origin` row stays: the conversation
     * a session was born in is not a binding an unlink can sever.
     *
     * Keyed on the `binding` the completed request named, never on the channel
     * alone: between the click and the response another tab can unlink A and
     * link B on the same channel, and the slots push for B can land here first.
     * The server deleted exactly A (it refuses anything else with 409), so this
     * removes exactly A's rows — a B row, same channel, different token, stays,
     * and the tab does not read as disconnected from a binding the server still
     * holds. The slot's `slack_*` fields describe the Slack THREAD row, so they
     * clear in the same write as that row and only then: a Slack row that
     * survives the compare keeps its fields.
     */
    dropSlotLinks(state, action: PayloadAction<{ key: string; channel: string; binding: string }>) {
      patchSlotRow(state, action.payload.key, slot => {
        if (!slot.links) return false
        const before = slot.links.length
        slot.links = slot.links.filter(candidate => !(
          candidate.channel === action.payload.channel
          && candidate.direction !== 'origin'
          && candidate.binding === action.payload.binding
        ))
        if (slot.links.length === before) return false
        if (action.payload.channel === 'slack') {
          slot.slack_linked = false
          slot.slack_channel = undefined
          slot.slack_thread_ts = undefined
        }
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
    /** Optimistic toggle of a user-owned mute flag: the "mute sessions it
     *  opens" rule (`mutes_opened`) or the per-row mute (`muted`). Unlike pin
     *  neither bears on sidebar ordering, so they need none of the pin
     *  batch/order machinery -- just flip the row and let the authoritative
     *  `slot_patch` frame reconcile. */
    updateSlotMuteFlag(state, action: PayloadAction<{ key: string; flag: MuteFlag; value: boolean }>) {
      const { key, flag, value } = action.payload
      patchSlotRow(state, key, slot => {
        slot[flag] = value
      })
      // A fresh attempt supersedes any prior failure for this flag on this row;
      // onError re-sets it if this one also fails.
      if (state.slotMuteFlagError) delete state.slotMuteFlagError[muteFlagErrorKey(flag, key)]
    },
    /** Record a mute toggle failure so the kebab menu can render it through
     *  ErrorNotice. Set after the optimistic flip has been rolled back. */
    setSlotMuteFlagError(state, action: PayloadAction<{ key: string; flag: MuteFlag; message: string }>) {
      state.slotMuteFlagError ??= {}  // partial preloaded state
      state.slotMuteFlagError[muteFlagErrorKey(action.payload.flag, action.payload.key)] = action.payload.message
    },
    /** Paint a pinned order. A bare list is the person's own gesture, shown
     *  before the gateway answers; `{order, rev}` is the gateway's answer,
     *  applied only if no newer revision has landed since. */
    setPinRanks(state, action: PayloadAction<string[] | { order: string[]; rev: number }>) {
      paintOrder(state, action.payload, ORDER_GATES[0], slot => !!slot.pinned)
    },
    setPendingPinOrder(state, action: PayloadAction<string[] | null>) { state.pendingPinOrder = action.payload },
    setPinOrderError(state, action: PayloadAction<string>) { state.pinOrderError = action.payload },
    /** Paint the manual order (sort "Manual"), the same two ways as
     *  `setPinRanks`. Every slot may hold a place, pinned or not: a pinned row
     *  draws in the pinned order and keeps its manual place for when it is
     *  unpinned. */
    setManualRanks(state, action: PayloadAction<string[] | { order: string[]; rev: number }>) {
      paintOrder(state, action.payload, ORDER_GATES[1], () => true)
    },
    setPendingManualOrder(state, action: PayloadAction<string[] | null>) { state.pendingManualOrder = action.payload },
    setManualOrderError(state, action: PayloadAction<string>) { state.manualOrderError = action.payload },
    triggerRefresh(state) { state.refreshTrigger += 1 },
    triggerCronRefresh(state) { state.cronRefreshTrigger += 1 },
    /** DUAL PAYLOAD SHAPE — the form IS the semantics. String payload =
     *  MANUAL reminder: records the relay-immune sentinel; only a local read
     *  in this window clears it. Object payload `{slot, ts?}` = message
     *  arrival: records a clearable watermark. Passing a bare string for an
     *  arrival creates a badge no remote read can retire — arrival call
     *  sites must always use the object form. */
    markSlotUnread(state, action: PayloadAction<string | { slot: string; ts?: string; localTs?: string }>) {
      const slot = typeof action.payload === 'string' ? action.payload : action.payload.slot
      const ts = typeof action.payload === 'string' ? undefined : action.payload.ts
      const localTs = typeof action.payload === 'string' ? undefined : action.payload.localTs
      if (!state.unreadSlots.includes(slot)) state.unreadSlots.push(slot)
      if (!state.unreadSince) state.unreadSince = {}  // partial preloaded state
      // Watermarks carry only ACTUAL server-minted message timestamps: a
      // frame without one falls back to the slot's last_ts, and when neither
      // exists nothing is recorded (any relayed read may clear). Minting
      // client time here would make windows disagree about the same message
      // and strand badges against valid relays.
      const effectiveTs = typeof action.payload === 'string'
        ? undefined
        : (ts ?? slotLastTsOf(state, slot))
      if (typeof action.payload !== 'string') {
        // ONE atomic shared write: badge presence and watermark are the same
        // record entry ('' = badge with no watermark, any relayed read
        // clears). The RMW keeps the newest instant, so publishing on every
        // arrival converges. The write is queued, not done here: arrivals
        // coalesce into one write per SHARED_UNREAD_FLUSH_MS, and every read
        // or drain in this window flushes the queue before it touches the
        // record.
        queueSharedUnread(slot, effectiveTs ?? '')
        const prev = state.unreadSince[slot]
        // `localTs` is the row's own server ts and guards THIS window only.
        // A row the gateway never saves (a permission row) publishes the
        // slot's saved-row watermark above, which a gateway restart cannot
        // strand; this window still keeps the row's own ts, so a trailing
        // read relayed from a window that saw only the rows before it cannot
        // clear the badge. A local Mark as read clears it regardless.
        const localWatermark = newerTs(effectiveTs, localTs)
        // A manual sentinel is never demoted by a message arrival; otherwise
        // the chronologically newest parseable instant wins.
        if (localWatermark !== undefined && prev !== MANUAL_UNREAD) {
          const next = newerTs(prev, localWatermark)
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
      // Relayed on every arrival, not only when the badge is new: the hub
      // zeroes an instance's count when the user selects it, and the next
      // arrival is what lights the chip again. A standalone window skips it.
      relayUnreadCount(state)
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
      const _readTs = slotLastTsOf(state, action.payload)
      clearSharedUnreadIfCovered(action.payload, _readTs)
      _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
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
      // The reader already cleared the shared record for what it read, so a
      // queued arrival this read covers must not be written after that clear.
      // Runs before the local guard below, which keeps this window's own badge.
      dropQueuedSharedUnreadCoveredBy(slot, readTs)
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
        _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
        return
      }
      if (state.unreadSince?.[slot] !== undefined) {
        // A sentinel never reaches here (readCovers rejects it above), so the
        // deleted key is always a shared message watermark.
        delete state.unreadSince[slot]
      }
      if (!state.unreadSlots.includes(slot)) return
      state.unreadSlots = state.unreadSlots.filter(k => k !== slot)
      _relayUnreadToParent(JSON.stringify(relayedUnreadKeys(state)))
    },
    setUpdateProgress(state, action: PayloadAction<{ step: string; detail: string } | null>) {
      state.updateProgress = action.payload
    },
    setDesktopUpdateAvailable(state, action: PayloadAction<boolean>) {
      state.desktopUpdateAvailable = action.payload
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
      .addCase(streamFlush, (state, action) => {
        for (const r of action.payload.recency) applySlotActivity(state, r)
      })
      .addCase(fetchSlots.pending, (state, action) => {
        if (!state.slotFetchesInFlight) state.slotFetchesInFlight = []
        state.slotFetchesInFlight.push(action.meta.requestId)
        // A new attempt (e.g. the reconnect catch-up) may still land a list.
        state.slotsFetchFailed = false
        // The reply cannot describe anything that happens from here on, so this
        // is the instant every later single-slot write outranks it from.
        if (!state.slotFetchWriteMark) state.slotFetchWriteMark = {}
        state.slotFetchWriteMark[action.meta.requestId] = {
          writeSeq: state.slotWriteSeq ?? 0,
          unreadDrainHeld: state.unreadDrainHeld,
          heldKeys: [...(state.slotsHeldKeys ?? [])],
          holdChannels: state.slotsHoldChannels,
          completeKeys: null,
        }
        // This request is newer than every add tracked so far, so it is the
        // read a kept session was waiting for.
        state.slotMemberReadWanted = false
      })
      .addCase(fetchSlots.fulfilled, (state, action) => {
        // A reply a confirmed close waited on past its cap is known to predate
        // that close's pop, so the closed key is filtered out of it — but ONLY
        // that key. Whatever else the reply carries (an unrelated rename, a
        // slot created meanwhile) is still the newest thing this transport has
        // said and still applies. Not gated on `slotsLoaded`: a pairing exists
        // only once this tab saw a close or a `removed` for the key, and a
        // reconnect clears the flag while such pairings (a 404 close awaiting
        // its outcome) are still live — an unfiltered pre-pop row would then
        // read as that close's rollback. On a cold boot nothing is paired, so
        // the first list still applies whole: an empty sidebar is worse than
        // one stale row.
        // `meta` is optional-chained for the hand-built actions in older tests.
        const requestId = action.meta?.requestId
        const requestMark = requestId ? state.slotFetchWriteMark?.[requestId] : undefined
        const staleKeys = requestId ? staleKeysForSlotFetch(state, requestId) : new Set<string>()
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
        // Keys ADDED while the request was in flight (see `membersOutranking`),
        // read from `slotMemberAt`. A membership add stamps membership only, not
        // content, so this is NOT a subset of `outranked`. Only these may be
        // re-added when the reply omits them; a content-only outranked key must
        // not be resurrected, since its row may since have been deleted by
        // another client (GPT F1).
        const addedWhileInFlight = membersOutranking(state, requestId)
        // For a stale key the reply's row is pre-pop and is NOT trusted, but the
        // key may since have been recreated (a resume on another client): keep
        // whatever row is on screen for it rather than dropping the key, so a
        // live same-key session and its unread state survive the stale reply.
        // `addedWhileInFlight` is included because a membership add stamps only
        // `slotMemberAt`, so a pure add (no content stamp) leaves `outranked`
        // empty yet still needs the live-row map to re-add its omitted key.
        const current = staleKeys.size || outranked.size || addedWhileInFlight.size
          ? new Map((state.slots ?? []).map(s => [s.key, s]))
          : undefined
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
        // A key MEMBERSHIP-added while this request was in flight, which the
        // reply OMITS, is a create / resume / fork / companion-open the server
        // serialized the reply too early to list (issue #14831). The whole-list
        // replace below would drop that freshly added row, so re-add its live
        // row — kept in reply order after the rest — and the sidebar does not
        // need a reload to learn about it. A key the reply contains is already
        // handled above; a content-only outranked key is deliberately NOT here,
        // so a reply cannot revive a chat deleted mid-flight.
        //
        // The re-add is bounded by the mark comparison in `membersOutranking`:
        // only a reply dispatched BEFORE the add re-adds the key. A later reply
        // (mark at or above the stamp) cannot re-add it, so a foreign delete that
        // omits the key wins on the next authoritative list — no resurrection
        // while the WS is disconnected (GPT F1). The #14831 case re-adds on every
        // pre-add reply — two `fetchSlots` can overlap — and heals on the next
        // authoritative list either way.
        if (current && addedWhileInFlight.size) {
          const present = new Set(rows.map(s => s.key))
          for (const key of addedWhileInFlight) {
            if (present.has(key)) continue
            const live = current.get(key)
            if (live) rows.push(live)
          }
        }
        // A reply in flight can be older than the live frames that arrived while
        // it travelled, so it may omit a slot the stream has since created. The
        // unread drain still runs — that is this path's documented job, and a
        // badge self-heals — but eviction is withheld once the stream is live.
        const fresh = !state.slotsLoaded
        // `awaitCloseOutcome` pairs every fetch already in flight at the 404, so
        // an unfiltered reply was requested afterwards and serialized after the
        // pop. Membership there is a live restored or recreated key, not a
        // straggler, and releases the awaiting hold without spending its budget.
        const replyKeys = new Set(rows.map((s: ChatSlot) => s.key))
        for (const key of Object.keys(state.closingSlots ?? {})) {
          const hold = state.closingSlots[key]
          if (hold.awaitingOutcome && replyKeys.has(key)) delete state.closingSlots[key]
        }
        applySlots(state, rows)
        state.slotsGeneration = (state.slotsGeneration ?? 0) + 1
        state.slotsLoaded = true
        // A complete frame that arrived while this request was in flight is the
        // newer, labelled list; the reply is older and carries no label. The drain
        // then runs against the union: a key the frame lists is live however
        // stale the reply, and a key both omit is genuinely gone. That is what
        // releases the hold this request was dispatched under. A key deleted
        // after the frame lingers until the next frame, which drains it.
        const completeKeys = requestMark?.completeKeys ?? null
        reconcileSlots(
          state,
          completeKeys ? new Set([...replyKeys, ...completeKeys]) : replyKeys,
          fresh,
          state.unreadDrainHeld || (requestMark?.unreadDrainHeld === true && completeKeys === null),
          [...(requestMark?.heldKeys ?? []), ...(state.slotsHeldKeys ?? [])],
          state.slotsHoldChannels || requestMark?.holdChannels === true,
        )
        settleUnlistedMembers(state, requestId, replyKeys)
        settleSlotFetch(state, requestId)
      })
      .addCase(fetchSlots.rejected, (state, action) => {
        settleUnlistedMembers(state, action.meta?.requestId, null)
        settleSlotFetch(state, action.meta?.requestId)
        if (!(state.slotFetchesInFlight ?? []).length) state.slotsFetchFailed = true
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
          if (!isUnsafeKey(action.payload.key)) {
            delete state.closingSlots?.[action.payload.key]
            delete state.durablyRemoved?.[action.payload.key]
          }
          if (!state.slots.find(s => s.key === action.payload.key)) { // row-read: membership test, adds a row rather than changing one
            state.slots.push(action.payload)
            relayUnreadIfHolding(state)
          }
          // A create is a single-slot MEMBERSHIP write, so stamp it as one (see
          // `stampSlotMember`): a `fetchSlots` reply already in flight was
          // serialized by the server before this slot existed and so omits the
          // key; the membership stamp lets `membersOutranking` re-add the row
          // the reply dropped (issue #14831). Stamped even when the row is
          // already present — the create still asserts the key is current as of
          // now, which a reply that predates it must not override.
          stampSlotMember(state, action.payload.key)
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

export const { sseStatus, sseYolo, setYoloDuration, sseConnected, sseDisconnected, sseSlots, sseSlotsPartial, sseSlotListHolds, setSidebarOrder, sseTodoUpdate, sseMcpReportUpdate, touchSlotActivity, setChannelTrusted, sseSlotTitle, sseSlotPatch, addSlotOptimistic, removeSlotOptimistic, releaseCloseHold, awaitCloseOutcome, expireCloseHold, confirmCloseHold, armConfirmedCloseHold, updateSlot, updateSlotFolder, updateSlotPin, updateSlotMuteFlag, setSlotMuteFlagError, triggerRefresh, triggerCronRefresh, markSlotUnread, markSlotRead, remoteSlotRead, setUpdateProgress,
  setDesktopUpdateAvailable, sseSlotColor, setSessionDefaultColor, setSessionColorsMode, setSessionColorsPalette, setSessionColorsIntensity, setEnabledAppIds, patchSlotSourceLinks, patchSlotLink, dropSlotLinks, setPinRanks, setPendingPinOrder, setPinOrderError, setManualRanks, setPendingManualOrder, setManualOrderError } = dashboardSlice.actions

export { slotSurfaceKey, slotIsRemoteBound } from './slotIdentity'

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
 *
 * The exception is an orphan no reconcile will drain: any orphan while the
 * gateway labels its lists partial, or one the gateway holds (`slotsHeldKeys`, or a
 * channel key under `slotsHoldChannels`). The premise above does not hold for it. Its unread entry is kept so the dot returns with the tab, but
 * it is left out of the badge, which would otherwise count a session the
 * sidebar cannot show.
 */
function countUnreadByMode(
  slots: ChatSlot[],
  unread: string[],
  mode: string,
  heldKeys: readonly string[] = [],
  holdChannels = false,
  listPartial = false,
): number {
  if (unread.length === 0) return 0
  const surfaceByKey = new Map(slots.map(s => [s.key, slotSurfaceKey(s)]))
  const isHeldOrphan = heldOrphanTest(new Set(surfaceByKey.keys()), heldKeys, holdChannels, listPartial)
  // A Crew Mode crew member is no row of the person's lists (its root's Crew
  // panel lists it), so its unread entry is kept for that panel but not counted.
  const crewMembers = crewMemberKeys(slots)
  // The chat surface ('') counts every slot the chat page renders, legacy
  // Autopilot slots included (see `isChatPageSurface`).
  const isChatSurface = mode === ''
  let count = 0
  for (const k of unread) {
    if (isHeldOrphan(k) || crewMembers.has(k)) continue
    const sk = surfaceByKey.get(k) ?? ''
    if (isChatSurface ? isChatPageSurface(sk) : sk === mode) count++
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
    // The Crew Mode preview decides whether crew members count (`crewMemberKeys`).
    // It is passed as an argument, not read inside an input selector: the
    // selector memoizes on its arguments, so a flip with the same state would
    // otherwise answer from the cache.
    const count = createSelector(
      (state: { dashboard: DashboardState }) => state.dashboard.slots,
      (state: { dashboard: DashboardState }) => state.dashboard.unreadSlots,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsHeldKeys,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsHoldChannels,
      (state: { dashboard: DashboardState }) => state.dashboard.slotsListPartial,
      (_state: { dashboard: DashboardState }, crewMode: boolean) => crewMode,
      (slots, unread, heldKeys, holdChannels, listPartial) =>
        countUnreadByMode(slots, unread, mode, heldKeys ?? [], holdChannels === true, listPartial === true),
    )
    sel = state => count(state, crewModeEnabled())
    _unreadByModeCache.set(mode, sel)
  }
  return sel
}

/**
 * True while the page has never seen a slot list and one may still arrive, so a
 * pane with no open session cannot yet tell "no chats" from "chats loading".
 * `slotsGeneration`, not `slotsLoaded`: a socket reconnect resets the latter,
 * and a gateway whose list already landed is not waited on again. A failed
 * fetch with none left in flight ends the wait.
 */
export const selectSlotListPending = (state: { dashboard: DashboardState }): boolean => {
  const d = state.dashboard
  return !d.slotsLoaded && (d.slotsGeneration ?? 0) === 0 && !d.slotsFetchFailed
}

export default dashboardSlice.reducer

/** True when the local bundled gateway's policy provider governs this install.
 * Shared by `usePolicyGovernedNotification`, `UpdateFoundModal`, `AboutPanel`. */
export function selectPolicyGoverned(s: { dashboard: DashboardState }): boolean {
  const st = s.dashboard.status
  return st?.update_bundled_by_app === true && st?.update_managed_by === 'command' && st?.update_can_apply === true
}
