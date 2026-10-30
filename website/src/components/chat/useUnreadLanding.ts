/**
 * Re-entering a crewmate chat lands on the last message the reader saw, not on
 * the newest one.
 *
 * A crewmate keeps talking while nobody is looking. The transcript's own
 * reading position (`ScrollAnchorCache`) covers a reader who left mid-history,
 * and deliberately clears at the bottom, so the common crewmate return -- "I
 * was caught up, then I left" -- opened at the live end with everything new
 * above the fold. This hook adds the missing half:
 *
 *   - while the reader is at the bottom, the newest message's durable id is
 *     saved as the slot's caught-up marker (`caughtUpMarker.ts`);
 *   - on entry, the marker saved by the PREVIOUS visit is resolved ONCE
 *     against the first hydrated transcript (`findUnreadRun`) and held for
 *     the visit: with unseen messages after it, the list draws its divider
 *     above the first one and this hook scrolls the divider to the top of the
 *     viewport, once, as soon as it is in the DOM.
 *
 * The run is a fact about the ENTRY. A message that arrives while the reader
 * is present is live, not unseen: it never opens a run, never moves the
 * divider and never lands the reader, whatever they were doing. A reader who
 * left mid-history has a saved reading position of their own; that position
 * outranks the landing (the divider still marks the run).
 *
 * The landing is a host-level scroll write, like the pinned prompt's jump
 * (`usePinnedPrompt.jumpToPinnedPromptInPlace`): it reads the divider's live
 * rect against the scroller's, so it is exact whatever the rows above are
 * still priced at, and the follow policy reads the resulting scroll event as
 * a reader leaving the bottom -- follow releases, the jump pill shows. Nothing
 * here touches the virtualizer's own entry placement; it runs after it and
 * only when there is a divider to land on.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { durableMessageId, findUnreadRun, loadSeenTail, markerOnHand, newestDurableMessage, saveSeenTail, type UnreadRun } from '../../chat-core/transcript/caughtUpMarker'
import { UNREAD_DIVIDER_ATTR } from '../../app-sdk/NewMessagesDivider'
import { DEFAULT_BOTTOM_THRESHOLD } from '../../hooks/virtualizer/FollowController'
import { loadScrollAnchor } from '../../hooks/virtualizer/ScrollAnchorCache'
import type { ChatMessage } from '../../types'
import type { DisplayItem, TurnItem } from '../../pages/chat/types'

/** Where the divider lands below the scroller's own top padding: one short
 *  bubble of the last message the reader DID see stays above the line, so the
 *  landing reads as "you were here" rather than as an arbitrary cut. */
export const UNREAD_LANDING_PAD_PX = 56

/** How many older pages the entry walks back looking for the marker's row when
 *  the hydrated page does not hold it. A crewmate that spoke more than a page
 *  since the last visit is the long absence this feature exists for, so the
 *  marker is looked for, not written off; the bound keeps a very long absence
 *  from paging the whole history. */
export const MARKER_WALK_MAX_PAGES = 4

/** How many frames the landing waits for the divider's row to mount after the
 *  window was pointed at it. A far start is teleported by `mountIndex`, and
 *  the row is in the DOM on the commit that follows; this bounds a row that
 *  never comes (the run resolved against rows the list then dropped). */
const LANDING_WAIT_FRAMES = 12

/** The display row holding the message with `id`, or -1. Walks the rows'
 *  messages the way the list grouped them, so a message folded into a turn is
 *  found through the turn. */
export function displayIndexOfMessage(items: readonly DisplayItem[], id: string): number {
  const holds = (t: TurnItem): boolean =>
    t.kind === 'single' ? durableMessageId(t.msg) === id : t.msgs.some((m) => durableMessageId(m) === id)
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    if (it.kind === 'turn' ? it.items.some(holds) : holds(it)) return i
  }
  return -1
}

/** One visit to one slot: what the entry decided and where the landing stands. */
interface Visit {
  slot: string
  /** The run resolved from the first hydrated transcript; `undefined` until
   *  one is on hand, then fixed for the visit. */
  run: UnreadRun | null | undefined
  /** Older pages asked for while the marker's row was not on hand. */
  pagesWalked: number
  /** The host has reported the reader OFF the bottom this visit: the next
   *  at-bottom report is the reader's own arrival, not the entry's default. */
  offBottomSeen: boolean
  /** The marker's row was never found within the walk: a row not loaded is not
   *  a row gone, so the stored marker is left alone -- until the reader first
   *  reaches the bottom of this visit. From there they have seen the newest row,
   *  the old marker is beyond reach and no longer needed, and saving resumes;
   *  otherwise one long absence would freeze the slot's marker for good. */
  markerUnresolved: boolean
  /** `owed` until the landing has run, or been decided unnecessary (no run,
   *  the reader's own position, a start row the list never mounts). Until
   *  then the host's `isAtBottom` is the transcript's default placement, not
   *  the reader's. */
  landing: 'owed' | 'done'
  /** True from a landing write that took the reader OFF the bottom band until
   *  the host reports them off it. The `isAtBottom` the host holds in the
   *  landing commit is stale -- the scroll event that corrects it lands a
   *  render later -- and saving on it would write the newest message as the
   *  marker the moment the reader was placed above it. A write the browser
   *  clamped, or one that stayed inside the band (the whole run already fits
   *  below the line), arms nothing: the reader IS at the bottom and sees it
   *  all, and the transcript may never report otherwise. */
  awaitFreshBottom: boolean
  /** The reader reached the bottom once in THIS visit: the run is read and the
   *  pill drops its count. Per visit, so a later visit to the same slot with a
   *  new run starts with the count on. */
  readThrough: boolean
}

export function useUnreadLanding(opts: {
  /** Only a crewmate's own thread lands this way; a split pane keeps the
   *  transcript's default entry placement. */
  enabled: boolean
  slotKey: string
  /** The transcript's `sessionId` for this slot (`pane:<slot>`), under which
   *  `ScrollAnchorCache` keeps the reader's own saved position. */
  anchorSessionId: string
  /** The slot's authoritative transcript page has landed in `messages`. A
   *  live frame can seed a partial list first (a reply posted before the chat
   *  opened); resolving the run against that would miss the marker's row and
   *  lock in "no run" for the visit. */
  hydrated: boolean
  /** Older history: whether more exists above the rows on hand, whether a page
   *  is in flight, and how to ask for the next one. The entry walks back a
   *  bounded number of pages when the marker's row is not on hand. */
  older: { hasMore: boolean; loading: boolean; load: (() => void) | null }
  /** A person can see the thread right now (window visible and focused,
   *  nothing covering the chat). A reply that lands at the bottom of a parked
   *  window is not read: the marker holds and the next visit lands on it. */
  shown: boolean
  messages: readonly ChatMessage[]
  isAtBottom: boolean
  scrollerRef: RefObject<HTMLDivElement | null>
  /** The grouped rows the list reported (`onDisplayItems`), read at landing
   *  time to point the window at a start row that is not mounted. */
  getDisplayItems: () => readonly DisplayItem[]
  mountIndex: (index: number) => void
  /** Whether the virtualizer's own anchored entry still owns the position
   *  (`VirtualTranscriptHandle.restoreGate`): while it does, the scroller is
   *  nobody's reading position yet, so nothing is saved from it. */
  restoreOwned: () => boolean
}): {
  /** The run the divider marks; fixed for the visit. */
  unreadRun: UnreadRun | null
  /** What the jump pill says: the run's count until the reader has reached the
   *  bottom once in this visit, then 0 (the plain arrow) -- the messages are
   *  read, the divider stays as the landmark. */
  unreadCount: number
} {
  const { enabled, slotKey, anchorSessionId, hydrated, older, shown, messages, isAtBottom, scrollerRef, getDisplayItems, mountIndex, restoreOwned } = opts

  // Read once per slot entry: the marker the PREVIOUS visit left (the save
  // below overwrites storage as soon as the reader reaches the bottom), and
  // whether the reader left this transcript at a position of their own, which
  // the virtualizer restores and the landing must not fight.
  const seenAtEntry = useMemo(() => (enabled ? loadSeenTail(slotKey) : null), [enabled, slotKey])
  const readerPositionAtEntry = useMemo(() => enabled && loadScrollAnchor(anchorSessionId) != null, [enabled, anchorSessionId])

  const visitRef = useRef<Visit | null>(null)
  let visit = visitRef.current
  if (!visit || visit.slot !== slotKey) {
    visit = { slot: slotKey, run: undefined, landing: 'owed', awaitFreshBottom: false, readThrough: false, pagesWalked: 0, markerUnresolved: false, offBottomSeen: false }
    visitRef.current = visit
  }
  // Resolved against the first HYDRATED transcript that holds the marker's row
  // and then fixed: a message arriving during the visit is live, not unseen.
  // A render-time latch so the divider is in the same commit the landing
  // effect reads. A marker beyond the bounded page is looked for in older
  // history (the effect below pages back); only once that walk is spent, or
  // there is no more history, is the row taken as gone.
  const markerMissing = hydrated && visit.run === undefined && !markerOnHand(messages, seenAtEntry)
  const canWalk = markerMissing && older.hasMore && older.load != null && visit.pagesWalked < MARKER_WALK_MAX_PAGES
  // A page still in flight may hold the row: the last page the walk asked for
  // lands after `pagesWalked` reached the bound, so the decision waits for it.
  const pageInFlight = markerMissing && older.loading && visit.pagesWalked > 0
  if (visit.run === undefined && hydrated && !canWalk && !pageInFlight) {
    visit.run = findUnreadRun(messages, seenAtEntry)
    // Spent the walk with history still above: the row is not loaded, not gone.
    if (markerMissing && older.hasMore) visit.markerUnresolved = true
  }
  const unreadRun = visit.run ?? null

  // The walk back: one older page at a time, each asked for once.
  useEffect(() => {
    if (!enabled || !canWalk || older.loading) return
    visit.pagesWalked += 1
    older.load?.()
  }, [enabled, canWalk, older, visit, messages])

  // Read-through lives on the visit; this only re-renders the pill when it flips.
  const [, bumpReadThrough] = useState(0)
  // The host's latest at-bottom report, for the landing's writes: a settle write
  // that lands AFTER the host has already reported the reader off the bottom has
  // nothing stale to wait out, and arming the wait then would leave it armed for
  // good (no later report flips a value that is already false).
  const isAtBottomRef = useRef(isAtBottom)
  isAtBottomRef.current = isAtBottom
  const unreadCount = unreadRun && !visit.readThrough ? unreadRun.count : 0

  useLayoutEffect(() => {
    if (!enabled || visit.landing === 'done' || visit.run === undefined) return
    // The entry decision, made once the transcript is on hand: no run (caught
    // up, no marker, or a marker whose row is gone) or a position the reader
    // chose themselves retires the landing for the whole visit.
    if (!unreadRun || readerPositionAtEntry) { visit.landing = 'done'; return }
    const sc = scrollerRef.current
    if (!sc) return
    const dividerIn = (root: HTMLElement) => root.querySelector<HTMLElement>(`[${UNREAD_DIVIDER_ATTR}]`)
    const place = (): 'missing' | 'landed' => {
      const d = dividerIn(sc)
      if (!d) return 'missing'
      // Past the scroller's own top padding (ChatPane pays the floating DM
      // header there), read live rather than copied.
      const padTop = parseFloat(getComputedStyle(sc).paddingTop) || 0
      const delta = d.getBoundingClientRect().top - sc.getBoundingClientRect().top - padTop - UNREAD_LANDING_PAD_PX
      if (Math.abs(delta) <= 1) return 'landed'
      const before = sc.scrollTop
      sc.scrollTop += delta
      // Only a write that really took the reader off the bottom band, while the
      // host still says "at bottom", is one its `isAtBottom` has yet to catch up
      // with (see `awaitFreshBottom`); a later settle write changes nothing the
      // host has not already reported.
      const offBottom = sc.scrollTop !== before && sc.scrollHeight - sc.clientHeight - sc.scrollTop > DEFAULT_BOTTOM_THRESHOLD
      if (offBottom && isAtBottomRef.current) visit.awaitFreshBottom = true
      return 'landed'
    }
    if (!dividerIn(sc)) {
      // The start row is outside the mounted window (a long unseen run): point
      // the window at it, then wait for the commit that mounts it. A start the
      // list holds no row for cannot be landed on at all.
      const idx = displayIndexOfMessage(getDisplayItems(), unreadRun.startId)
      if (idx < 0) { visit.landing = 'done'; return }
      mountIndex(idx)
    }
    if (place() === 'landed') visit.landing = 'done'
    // One more frame after the first landing: the first write runs against a
    // row whose neighbours may still be estimates, and the virtualizer's own
    // post-entry pin fires on the frame after items arrive; a second read of
    // the live rect settles both. A divider still to mount is awaited for a
    // bounded number of frames; the landing stays OWED meanwhile, so no save
    // reads the entry's default placement as the reader's.
    let frames = 0
    let raf = 0
    const tick = () => {
      raf = 0
      const r = place()
      frames += 1
      if (r === 'landed') visit.landing = 'done'
      if (r === 'missing' ? frames < LANDING_WAIT_FRAMES : frames < 2) raf = requestAnimationFrame(tick)
      else visit.landing = 'done'
    }
    raf = requestAnimationFrame(tick)
    return () => { if (raf) cancelAnimationFrame(raf) }
  }, [enabled, unreadRun, readerPositionAtEntry, visit, messages, scrollerRef, getDisplayItems, mountIndex])

  // The marker for the NEXT visit: the newest message while the reader is at
  // the bottom of a thread they can SEE (`shown`: a reply landing at the
  // bottom of a parked or covered window is not read, and the window coming
  // back re-runs this). Decided on the scroller's LIVE geometry, not on the host's
  // `isAtBottom` alone: that value is the entry's default until the
  // virtualizer's at-bottom report lands a render later, and a slot switch that
  // keeps the pane mounted carries the previous slot's value over. The prop is
  // what re-runs this when the reader arrives at the bottom; the geometry is
  // what says they are there. Not while the landing is still owed, not while
  // the virtualizer's own anchored entry still owns the position (a reader who
  // left mid-history is being put back there), and not on the at-bottom value
  // a landing write has not yet corrected (see `Visit`): the first report of
  // the reader OFF the bottom retires that wait, and their next arrival at the
  // bottom saves -- and reads the run through, which takes the count off the
  // pill.
  useEffect(() => {
    if (!enabled || visit.landing !== 'done') return
    if (visit.awaitFreshBottom) {
      if (isAtBottom) return
      visit.awaitFreshBottom = false
      return
    }
    if (!isAtBottom) { visit.offBottomSeen = true; return }
    if (!shown || restoreOwned()) return
    const sc = scrollerRef.current
    if (sc && sc.scrollHeight - sc.clientHeight - sc.scrollTop > DEFAULT_BOTTOM_THRESHOLD) return
    // An unresolved marker holds until the reader has REACHED the bottom this
    // visit; the entry's default placement is not that. The landing stood down
    // with no run, so only a report that arrived after it counts as the
    // reader's own (see `Visit.markerUnresolved`).
    if (visit.markerUnresolved) {
      if (!visit.offBottomSeen) return
      visit.markerUnresolved = false
    }
    // The newest row with a SERVER identity: an optimistic or queued send is
    // renamed when its echo lands, so a marker saved from it would match
    // nothing on the next visit (`isProvisionalRow`).
    const last = newestDurableMessage(messages)
    const id = last ? durableMessageId(last) : null
    if (!id) return
    saveSeenTail(slotKey, id)
    if (unreadRun && !visit.readThrough) {
      visit.readThrough = true
      bumpReadThrough((n) => n + 1)
    }
  }, [enabled, isAtBottom, shown, messages, slotKey, unreadRun, visit, scrollerRef, restoreOwned])

  return { unreadRun, unreadCount }
}
