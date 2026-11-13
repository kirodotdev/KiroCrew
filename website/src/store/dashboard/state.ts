import type { ChatSlot, StatusData } from '../../types'
import type { DefaultColorSetting, IntensityName, PaletteName, SessionColorMode } from '../../utils/sessionColors'
import type { OfflineCause, UnlistedMember } from '../dashboardSlice'
import type { CloseHold } from './closeHold'
import type { CloseWatchState } from './closeWatch'

/**
 * The shape of the dashboard slice's state. `../dashboardSlice` owns the
 * reducers that write it and re-exports this type, so callers import
 * `DashboardState` from there.
 */
export interface DashboardState extends CloseWatchState {
  status: StatusData | null
  /** The ad-hoc auto-approve duration this tab last saved in Settings, or
   *  undefined when it has saved none. Applied over every status write: the
   *  save is the newest fact this tab holds, and a status reply that began
   *  before it (the boot read, a slow earlier request) can carry the older
   *  value. Reset by a page load, whose boot read then reads the stored one. */
  savedYoloDuration?: NonNullable<StatusData['yolo_duration']>
  connected: boolean
  /** Why the socket is down when the Kiro relay said so (see
   *  `hooks/websocket/relayClose.ts`); null while connected or for a plain
   *  drop. */
  offlineCause: OfflineCause | null
  firstSocketPending: boolean // true until this page's first socket opens or closes: see useGatewayConnecting
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
  /** Per key, how many close holds reached the confirmed phase. Outlives the
   *  hold, so a close's late callback can tell a newer close was confirmed. */
  closeConfirmations: Record<string, number>
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
