/**
 * The automatic older-history walk for a `VirtualTranscript` host.
 *
 * A reader climbing to the top of a transcript that still has history behind it
 * is asking for that history; making them press the "Load earlier messages" bar
 * once per page is the defect this hook removes for every transcript host (the
 * crewmate DM on the Crewmates page first). The bar stays mounted: it is the
 * visible statement that history exists, the control for a scroll method the
 * walk cannot observe, and the retry after a failed page.
 *
 * The gates are the main page's (`pages/chat/page/transcriptPaging.ts`), read
 * from the same predicates and constants in `pages/chat/pagination.ts`, because
 * each one closes a door that was measured open on a real device:
 *
 *  - the bar must be on screen (`earlierAffordanceInView`): the one trigger with
 *    no second cause;
 *  - a REAL gesture on the scroller within `REAL_GESTURE_AUTH_MS` authorizes a
 *    BOUNDED run of pages (`OLDER_WALK_MAX_PAGES_PER_INPUT`), and that budget is
 *    spent, never reissued by a re-render;
 *  - the scroller must be settled (`OLDER_LANDING_SETTLE_MS` since its last
 *    scroll event) but the reader still recently active (`OLDER_WALK_ACTIVE_MS`
 *    since their last scroll or real input, so a wheel against the top edge,
 *    which scrolls nothing, still counts): a landing's prepend compensation
 *    writes scrollTop, and a page landing under a reader who stopped reads as
 *    the transcript moving on its own;
 *  - a bottom-followed reader is reading the live end, not asking for history;
 *  - every mounted row is measured, so a landing never piles estimate
 *    corrections over the reader;
 *  - a transcript too short to scroll fills without a gesture, because no
 *    scrollbar exists to climb (`shouldAutoFillOlder`), but it spends the same
 *    page budget: a page the host filters down to no visible rows leaves the
 *    transcript exactly as short, so geometry alone would page the whole
 *    history unattended.
 *
 * The host's `earlier` model owns the fetch, its in-flight flag and its failure;
 * the walk only decides WHEN to call `onLoad`, so a failed page parks the walk
 * on the bar's retry and never spins on a dead link.
 */
import { useEffect, useRef, type MutableRefObject, type RefObject } from 'react'
import {
  EARLIER_BAR_SELECTOR, earlierAffordanceInView, shouldContinueOlderWalk, shouldAutoFillOlder,
  OLDER_TOP_POLL_MS, OLDER_WALK_ACTIVE_MS, OLDER_LANDING_SETTLE_MS, OLDER_FILL_SLACK_PX, REAL_GESTURE_AUTH_MS,
} from '../../pages/chat/pagination'

/** The slice of the host's paging model the walk reads. */
export interface EarlierWalkSource {
  hasMore: boolean
  loading: boolean
  failed: boolean
  onLoad: () => void
}

export interface EarlierHistoryWalkOptions {
  scrollerRef: RefObject<HTMLElement | null> | MutableRefObject<HTMLElement | null>
  /** Reader intent belongs only to this transcript session. */
  sessionId: string
  /** `undefined` (no paging model) or `hasMore: false` disarms the walk. */
  earlier: EarlierWalkSource | undefined
  /** Stick-to-bottom state; a followed reader is never walked. */
  getFollow: () => boolean
  /** Rows mounted by the host, including overscan; only these must be measured. */
  getMountedIndices: () => readonly number[]
  /** Whether the measure farm holds a real height for a row. */
  farmIsMeasured: (index: number) => boolean
  /** Rows currently in the list; zero means "not loaded yet", never "short". */
  itemCount: number
}

export function useEarlierHistoryWalk({ scrollerRef, sessionId, earlier, getFollow, getMountedIndices, farmIsMeasured, itemCount }: EarlierHistoryWalkOptions): void {
  // Everything the poll reads lives in refs so the interval and the input
  // listeners do not re-arm on ordinary renders: a budget or a "last input"
  // stamp re-created by a render is an authorization nobody gestured for.
  const earlierRef = useRef(earlier)
  earlierRef.current = earlier
  const getFollowRef = useRef(getFollow)
  getFollowRef.current = getFollow
  const getMountedIndicesRef = useRef(getMountedIndices)
  getMountedIndicesRef.current = getMountedIndices
  const farmIsMeasuredRef = useRef(farmIsMeasured)
  farmIsMeasuredRef.current = farmIsMeasured
  const itemCountRef = useRef(itemCount)
  itemCountRef.current = itemCount

  const lastRealInputAtRef = useRef(0)
  const pagesSinceInputRef = useRef(0)
  const lastLandingAtRef = useRef(0)

  // A transcript entry is not a request for history. Clear the outgoing
  // session's intent even when the same scroller stays mounted.
  useEffect(() => {
    lastRealInputAtRef.current = 0
    pagesSinceInputRef.current = 0
    lastLandingAtRef.current = 0
  }, [sessionId])

  const armed = !!earlier?.hasMore
  useEffect(() => {
    const el = scrollerRef.current
    if (!armed || !el) return

    // Real input only. Writing `scrollTop` fires none of these, so a landing's
    // own compensation cannot authorize the next page. `keydown` is bound to
    // the SCROLLER, so typing in the composer is not an intent to read history.
    const noteInput = () => {
      lastRealInputAtRef.current = Date.now()
      pagesSinceInputRef.current = 0
    }
    // Any scroll event, momentum coasting included, marks the scroller as
    // still MOVING; pages land only once it is still.
    let lastScrollAt = 0
    const noteScroll = () => { lastScrollAt = Date.now() }
    let prevLoading = false

    const inputEvents = ['wheel', 'touchmove', 'keydown', 'pointerdown'] as const
    for (const ev of inputEvents) el.addEventListener(ev, noteInput, { passive: true })
    el.addEventListener('scroll', noteScroll, { passive: true })

    const tick = () => {
      const src = earlierRef.current
      if (!src?.hasMore) return
      if (el.clientHeight <= 0) return
      // A page LANDED when the host's in-flight flag fell. The walk is "in
      // progress" while the newest landing postdates the newest input; that is
      // what keeps it going after a landing's compensation moved the reader
      // thousands of px off the near-top gate.
      if (prevLoading && !src.loading) lastLandingAtRef.current = Date.now()
      prevLoading = src.loading
      if (src.loading || src.failed) return
      // An empty list is "not loaded yet", which the geometry branch below
      // cannot tell from "too short to scroll".
      const nRows = itemCountRef.current
      if (nRows === 0) return
      const now = Date.now()
      const tooShort = el.scrollHeight <= el.clientHeight + OLDER_FILL_SLACK_PX
      const sawInput = shouldAutoFillOlder({
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
        sawInput: now - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
      })
      // The short-transcript fill skips the gesture requirement but not the
      // budget. A landed page need not make the transcript taller (a host may
      // filter it down to no visible rows), so the fill stops after one
      // budget's worth of pages; the input events above still fire on a
      // scroller that cannot scroll, so a reader who wants more refills it.
      if (!shouldContinueOlderWalk({
        sawRealInput: sawInput,
        nearTop: el.scrollTop <= el.clientHeight,
        walking: lastLandingAtRef.current > lastRealInputAtRef.current,
        pagesSinceInput: pagesSinceInputRef.current,
      })) return
      if (!tooShort) {
        if (getFollowRef.current()) return
        // Settled means no scroll event lately: that is what a landing's
        // compensation write would fight.
        if (now - lastScrollAt < OLDER_LANDING_SETTLE_MS) return
        // Active means a scroll OR a real input lately: a wheel against the
        // top edge scrolls nothing but is still a reader asking for more.
        const lastActivityAt = Math.max(lastScrollAt, lastRealInputAtRef.current)
        if (now - lastActivityAt > OLDER_WALK_ACTIVE_MS) return
      }
      const bar = el.querySelector(EARLIER_BAR_SELECTOR)
      const vr = el.getBoundingClientRect()
      const br = bar?.getBoundingClientRect()
      if (!earlierAffordanceInView(
        br ? { top: br.top, bottom: br.bottom } : null,
        { top: vr.top, bottom: vr.bottom },
      )) return
      for (const index of getMountedIndicesRef.current()) { if (!farmIsMeasuredRef.current(index)) return }
      pagesSinceInputRef.current += 1
      prevLoading = true
      src.onLoad()
    }
    const t = setInterval(tick, OLDER_TOP_POLL_MS)
    return () => {
      clearInterval(t)
      for (const ev of inputEvents) el.removeEventListener(ev, noteInput)
      el.removeEventListener('scroll', noteScroll)
    }
  }, [armed, scrollerRef, sessionId])
}
