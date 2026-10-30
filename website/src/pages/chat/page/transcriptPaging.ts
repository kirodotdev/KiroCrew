import { useRef, useEffect, useCallback, useLayoutEffect, useState, type RefObject, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { store, type AppDispatch } from '../../../store'
import type { PinnedPromptState } from '../../../utils/pinnedPrompt'
import { loadOlderMessages, abortActiveOlderFetch } from '../../../store/chatSlice'
import type { DisplayItem } from '../types'
import {
  EARLIER_BAR_SELECTOR, earlierAffordanceInView, shouldPaginateOlder, OLDER_WALK_MAX_PAGES_PER_INPUT, shouldContinueOlderWalk, shouldAutoFillOlder,
  OLDER_TOP_POLL_MS, OLDER_WALK_ACTIVE_MS, OLDER_LANDING_SETTLE_MS, OLDER_FILL_SLACK_PX, REAL_GESTURE_AUTH_MS,
} from '../pagination'
import { inspectorOn, devLog } from '../../../dev/scrollInspector'
import { type SelectionEndpoints, restoreEndpointToTranscript, rowEndpoints, nextRetainedRange } from '../../../utils/selectionRetention'
import { useVirtualChat } from '../../../hooks/virtualizer/useVirtualChat'
import { slashRefusal, type RefusedPressAction } from './turnActions'

// The walk's gates and tunings live in pagination.ts so the pane transcript's
// walk (chat-core/transcript/useEarlierHistoryWalk) reads the same numbers.
export { shouldAutoFillOlder } from '../pagination'

/** Min gap between scroll-gesture-driven retries of a failed older-history
 * page. One request per gesture on a dead link, not one per scroll event. */
const OLDER_RETRY_COOLDOWN_MS = 1500

interface TranscriptScrollRefsOptions {
  activeSlot: string | null
}

/**
 * The refs that bridge the handlers declared before the virtualizer to its API:
 * follow state, the scroller element, the real-gesture stamps
 * and page budgets that authorise automatic history fetches (cleared on every slot
 * entry), and the mirrored virtualizer callbacks.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useTranscriptScrollRefs({
  activeSlot,
}: TranscriptScrollRefsOptions) {
  // Single scroll controller: the virtualizer (`virt`, created below) owns
  // follow + scroll-to-bottom. These refs bridge the early effects/handlers
  // (declared before `virt` in source order) to the virtualizer's API without
  // a temporal-dead-zone hazard — they are populated right after `virt` is
  // created and only read inside callbacks/effects that run post-render.
  // `vGetFollowRef` defaults to "following" so a gate that fires on the very
  // first commit (before the mirror populates it) behaves like the fresh-slot
  // bottom pin it accompanies.
  const vGetFollowRef = useRef<() => boolean>(() => true)
  // Live scroller element for gates that run before `virt` exists in scope
  // (handleTopReached is a dependency of the useVirtualChat call itself).
  const vScrollerElRef = useRef<HTMLElement | null>(null)
  // Whether THIS session has seen real reader input (wheel/touch) on the
  // scroller. Programmatic scrolls (landing compensation, bottom pins) fire
  // scroll events but neither of these, so the flag genuinely means "the
  // reader drove". Read by every self-issued history-fetch gate.
  // Timestamp of the last REAL gesture (wheel/touchmove). Separate from the
  // latch above because the idle prefetch's authorization must expire.
  const lastRealInputAtRef = useRef(0)
  // Pages the SENTINEL door has issued since the last real gesture. The walk
  // poll bounds itself the same way (OLDER_WALK_MAX_PAGES_PER_INPUT) because an
  // unbounded authorization once walked a whole multi-megabyte transcript over a
  // parked reader; this door needs its own counter because the poll's lives
  // inside its interval effect, out of reach here.
  const sentinelPagesSinceInputRef = useRef(0)
  // The walk poll's own budget and last-gesture stamp. Refs so neither survives
  // only as long as the effect that reads them -- see the note at their use.
  const walkPagesSinceInputRef = useRef(0)
  const walkLastInputAtRef = useRef(Number.NEGATIVE_INFINITY)
  // Entering a session is not a request for history, so a session must never
  // INHERIT authorization. Every one of these is written in exactly one place --
  // `noteInput`, on a real wheel/touchmove over the transcript -- and that
  // listener's effect is not keyed on the slot, so without this clear a gesture
  // in the session you just left still authorizes the automatic doors in the one
  // you just opened. Reader intent belongs to the session it happened in.
  //
  // A one-way "has this session ever seen input" latch used to sit here too. It
  // is gone rather than reset: an authorization that can only ever turn ON is not
  // an authorization, and both automatic doors now read the same EXPIRING window,
  // so leaving the slot simply lets it age out.
  useEffect(() => {
    lastRealInputAtRef.current = 0
    walkPagesSinceInputRef.current = 0
    walkLastInputAtRef.current = Number.NEGATIVE_INFINITY
    sentinelPagesSinceInputRef.current = 0
  }, [activeSlot])
  const vScrollToBottomRef = useRef<(behavior?: ScrollBehavior) => void>(() => {})
  // Mirrored so the early handlers (declared above `virt`) can refuse to page on
  // unsettled geometry, the same gate the walk poll and idle prefetch apply.
  const vFarmIsMeasuredRef = useRef<((i: number) => boolean) | null>(null)
  // Mirrored for the interval bodies, which must not re-arm per render.
  const earlierBarInViewRef = useRef<() => boolean>(() => false)
  const mountIndexRef = useRef<(index: number, opts?: { unionOnly?: boolean }) => boolean>(() => false)
  const estimateRowTopRef = useRef<(index: number) => number | null>(() => null)
  return {
    vGetFollowRef, vScrollerElRef, lastRealInputAtRef, sentinelPagesSinceInputRef, walkPagesSinceInputRef, walkLastInputAtRef, vScrollToBottomRef, vFarmIsMeasuredRef, earlierBarInViewRef,
    mountIndexRef, estimateRowTopRef,
  }
}

interface TranscriptPagingOptions {
  dispatch: AppDispatch
  activeSlot: string | null
  slotRunning: boolean
  slotHasMore: boolean
  scrollerRef: RefObject<HTMLDivElement>
  canMeasure: () => boolean
  scrollerWidthBucket: number
  vGetFollowRef: MutableRefObject<() => boolean>
  vScrollerElRef: MutableRefObject<HTMLElement | null>
  lastRealInputAtRef: MutableRefObject<number>
  sentinelPagesSinceInputRef: MutableRefObject<number>
  walkPagesSinceInputRef: MutableRefObject<number>
  walkLastInputAtRef: MutableRefObject<number>
  vScrollToBottomRef: MutableRefObject<(behavior?: ScrollBehavior) => void>
  vFarmIsMeasuredRef: MutableRefObject<((i: number) => boolean) | null>
  earlierBarInViewRef: MutableRefObject<() => boolean>
  mountIndexRef: MutableRefObject<(index: number, opts?: { unionOnly?: boolean; }) => boolean>
  estimateRowTopRef: MutableRefObject<(index: number) => number | null>
  displayItemsRef: MutableRefObject<DisplayItem[]>
  setPinExpanded: Dispatch<SetStateAction<boolean>>
  setPinned: Dispatch<SetStateAction<PinnedPromptState | null>>
  updatePinnedPrompt: () => void
  isStreaming: boolean
  setRefusedPress: Dispatch<SetStateAction<{ action: RefusedPressAction; message: string; } | null>>
  renderedDisplayItems: DisplayItem[]
  anchorAltId: (it: DisplayItem, index: number) => string
  prefetchStartIndex: number | undefined
  stableAnchorId: (it: DisplayItem, index: number) => string
  virtualKey: (it: DisplayItem, i: number) => string
}

/**
 * The transcript virtualizer and every door that pages older history into it: the
 * top sentinel, the earlier-messages bar, the top-parked walk poll and the retry on a
 * failed page, the selection retention across row recycling, the spinner placement,
 * and the slash-command refusal report.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useTranscriptPaging({
  dispatch, activeSlot, slotRunning, slotHasMore, scrollerRef, canMeasure, scrollerWidthBucket, vGetFollowRef, vScrollerElRef, lastRealInputAtRef, sentinelPagesSinceInputRef, walkPagesSinceInputRef,
  walkLastInputAtRef, vScrollToBottomRef, vFarmIsMeasuredRef, earlierBarInViewRef, mountIndexRef, estimateRowTopRef, displayItemsRef, setPinExpanded, setPinned, updatePinnedPrompt, isStreaming,
  setRefusedPress, renderedDisplayItems, anchorAltId, prefetchStartIndex, stableAnchorId, virtualKey,
}: TranscriptPagingOptions) {
  // (Sticky widget detection removed — widgets now unmount with the
  // window like any other item. See useVirtualChat call below for the
  // memory-vs-flicker trade-off rationale.)

  // THE admission rule for every AUTOMATIC older fetch: the control that offers
  // history must be on the reader's screen. See earlierAffordanceInView for why
  // this replaces the per-trigger geometry/latch proxies, each of which had a
  // second cause that was not the reader.
  const earlierBarInView = useCallback(() => {
    const el = vScrollerElRef.current
    if (!el) return false
    const bar = el.querySelector(EARLIER_BAR_SELECTOR)
    const vr = el.getBoundingClientRect()
    const br = bar?.getBoundingClientRect()
    return earlierAffordanceInView(
      br ? { top: br.top, bottom: br.bottom } : null,
      { top: vr.top, bottom: vr.bottom },
    )
  }, [vScrollerElRef])

  // Reaching the top of a resumed transcript fetches the history behind the loaded slice.
  /**
   * Has the active session finished ARRIVING? Every automatic history fetch is
   * shut until it has.
   *
   * This gates the door itself rather than feeding shouldAutoFillOlder, because
   * an empty transcript satisfies that predicate's "too short to scroll" branch
   * on GEOMETRY alone -- the one path no gesture requirement can close, since
   * the branch returns before it ever reads `sawInput`. A switch installs an
   * empty list, restores cursor ownership on fulfilment, and leaves the earlier
   * bar sitting in view with nothing above it: three conditions that together
   * read as "the reader is at the top asking for history" while the reader has
   * done nothing at all.
   */
  const handleTopReached = useCallback(() => {
    const chat = store.getState().chat
    if (!shouldPaginateOlder({ loadingOlder: chat.loadingOlder, slotHasMore: chat.slotHasMore })) return
    // A bottom-FOLLOWED reader on a SCROLLABLE transcript did not ask for
    // this: the top sentinel only reaches them through boot/measurement
    // transients (estimate-priced rows keep total height under a viewport
    // for a beat; a spacer collapse pulls the top within reach), and each
    // self-issued landing re-fires the transition -- a page chain over a
    // parked reader, felt as 'it starts loading previous the moment I
    // refresh, then jumps'. When the transcript cannot scroll at all the
    // fill is load-bearing (a short resumed session has no scrollbar to
    // climb), so it stays; the moment it is scrollable, further history
    // is reader-initiated (climb releases follow, sentinel then serves).
    if (!earlierBarInView()) return
    const el = vScrollerElRef.current
    // The "too short to scroll" branch of shouldAutoFillOlder fires on GEOMETRY,
    // so it also fires on a geometry TRANSIENT — and the comment above names the
    // two that produce one. What it did not account for is that the composer's
    // text is ChatPage state, so every keystroke re-renders this tree and gives
    // the virtualizer another chance to be caught mid-measurement. Reported from
    // a phone as history loading while TYPING. Both the walk poll and the idle
    // prefetch already refuse to page on unsettled geometry; this door did not,
    // and it is the one the sentinel comes through.
    // An empty transcript is NOT "too short to scroll" -- it is "not loaded yet".
    // shouldAutoFillOlder cannot tell the two apart: both satisfy its geometry
    // branch, which returns before it ever reads `sawInput`, so no authorization
    // requirement can close that path. Separating the two states is what stops a
    // session ENTRY from reading as a reader parked at the top asking for history.
    // The measured-rows loop below cannot do it either: over zero rows it checks
    // nothing and falls straight through.
    if (displayItemsRef.current.length === 0) return
    const nRows = displayItemsRef.current.length
    for (let i = 0; i < nRows; i++) { if (!vFarmIsMeasuredRef.current?.(i)) return }
    // Neither of the two obvious signals can key this. A one-way input latch is
    // one-way latch, so on a scrollable transcript one touch leaves it open for
    // the rest of the mount — it cannot mean "this session". And `!follow` is the
    // design shouldAutoFillOlder's own contract names as falsified: follow is
    // released with no reader input at all, both by an anchor restore and at slot
    // entry, where `lastWriteTop` resets to -1 so the idle branch's self-check
    // cannot rescue it. Either one turns an entry geometry transient into a fetch
    // nobody asked for.
    // What survives both is the EXPIRING form of the real-gesture record, which
    // the automatic doors already apply: a timestamp cannot latch open, and it is
    // silent on a slot the reader has not touched.
    if (el && !shouldAutoFillOlder({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      sawInput: Date.now() - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
    })) return
    // A gesture authorizes a BOUNDED run of pages, not the whole authorization
    // window. Authorization alone is what let one flick chain prepends until the
    // transcript ran out and left the reader at the very start of history.
    // The short-transcript fill is exempt because it bounds itself: every page
    // makes the transcript taller, so the geometry branch stops admitting once it
    // outgrows the viewport, and bounding it would strand a transcript that is
    // still too short to offer a scrollbar.
    if (el && el.scrollHeight > el.clientHeight + OLDER_FILL_SLACK_PX) {
      if (sentinelPagesSinceInputRef.current >= OLDER_WALK_MAX_PAGES_PER_INPUT) return
      sentinelPagesSinceInputRef.current += 1
    }
    if (inspectorOn()) devLog('OLDER', 'sentinel')
    void dispatch(loadOlderMessages())
  }, [dispatch, earlierBarInView, displayItemsRef, lastRealInputAtRef, sentinelPagesSinceInputRef, vFarmIsMeasuredRef, vScrollerElRef])
  /**
   * The click path needs no gate beyond the in-flight check the thunk already makes.
   *
   * Also the remedy for affordances NOT adjacent to it: the unavailable fork/plan items
   * and the partial-scope search count both name "load earlier history" as the fix while
   * that control sits at the top of the transcript. Those callers page from where the
   * statement is read, and this deliberately does NOT scroll or move focus -- the reader
   * is mid-transcript at the message they mean to fork, or typing in the search field,
   * and satisfying the condition takes many pages, so relocating them on each one costs
   * more than the hunt it saves. Their in-flight cue is a spinner on the item instead.
   */
  const handleLoadEarlier = useCallback(() => {
    if (store.getState().chat.loadingOlder) return
    if (inspectorOn()) devLog('OLDER', 'manual-bar')
    void dispatch(loadOlderMessages())
  }, [dispatch])

  const virt = useVirtualChat<DisplayItem>({
    items: renderedDisplayItems,
    getKey: virtualKey,
    // Anchor resolution survives the per-landing key reshuffle by identifying
    // rows by their TAIL message — see stableAnchorIdFor.
    getStableId: stableAnchorId,
    getAltId: anchorAltId,
    prefetchStartIndex,
    sessionId: activeSlot ?? '__no_slot__',
    // Width-bucketed height scope: measured row heights are only valid for
    // the width they were measured at. Breakout tables use the full pane, so
    // the uncapped 16px buckets re-measure desktop resizes as well as phones.
    // Colon-delimited, because the persisted key is `vc_heights_<scope>` and
    // storageGc reads the owning session as the segment before the first ':'
    // (and deletes a session's keys at that delimiter): a scope joined with
    // any other separator is orphaned on every boot and missed on delete.
    heightScopeKey: `${activeSlot ?? '__no_slot__'}:tables1:w${scrollerWidthBucket}`,
    canMeasure,
    estimatedHeight: 100,
    // Overscan tradeoff (experimental):
    //   smaller (3)   → least memory, frequent widget remounts on small scrolls
    //   medium  (12)  → screenful of buffer, ~290MB baseline / 450MB while scrolling
    //   larger  (25)  → fewer remounts but inflated RAM from warm iframe pool
    // Currently testing 6 — middle ground between memory and remount frequency.
    overscan: 6,
    // A first measurement lands in the offset tree immediately instead of
    // waiting out the height-sync debounce. Without this, a fast scroll or a
    // FAR jump mounts a streak of rows whose real heights sit outside the
    // spacer math for up to the debounce window; when they reconcile, content
    // shifts under the viewport. Chrome's native scroll anchoring absorbs
    // that shift, iOS Safari has none — measured 13-25px of post-jump drift
    // with anchoring disabled (the "jump lands off by a bit" report). First
    // measurements happen once per row, so they cannot be the oscillation the
    // debounce exists to smother.
    eagerFirstMeasure: true,
    // No isSticky: widget messages unmount along with everything else
    // when they leave the viewport window. Trade-off: scrolling back to
    // an old widget causes its iframe to reload (1-2 frames of flicker).
    // Memory benefit: only widgets in the active window are kept alive,
    // ~290MB baseline instead of 500MB+ with all-widgets-sticky.
    externalScrollerRef: scrollerRef,
    // The currently-streaming message is always the LAST message and
    // therefore always ends up in the LAST displayItems entry — whether
    // that entry is itself the streaming `single`, or a `turn`/`group`
    // that the streaming message got folded into (turns only close when a
    // new user/nudge message opens the next one, by which point the prior
    // streaming message has already finished). Passing its index lets the
    // virtualizer track that one row's growth every RO tick instead of
    // debouncing it into a stale-then-jump spacer (see the `streamingIndex`
    // option's doc and useVirtualChat.spacerLurch.test.tsx).
    streamingIndex: isStreaming && renderedDisplayItems.length > 0 ? renderedDisplayItems.length - 1 : undefined,
    // `slotRunning`, not `isStreaming`: a turn spends much of its life in tool
    // calls with no streaming row named, and follow has to keep working there.
    runActive: !!slotRunning,
    onTopReached: handleTopReached,
  })
  const { retainRange } = virt

  // Keep the rows under a transcript selection mounted while a touch handle
  // scrolls, so an off-screen start is not unmounted and re-rooted, and put a
  // handle that lands on the title or composer back on the transcript.
  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    // Where this selection last sat on rows; cleared when it collapses.
    let lastOnRows: SelectionEndpoints | null = null
    const syncSelectionRetention = () => {
      const selection = window.getSelection()
      // The restore fires its own selectionchange, which retains the result.
      if (selection && restoreEndpointToTranscript(scroller, selection, lastOnRows)) return
      if (!selection || selection.isCollapsed) lastOnRows = null
      else lastOnRows = rowEndpoints(scroller, selection) ?? lastOnRows
      const next = nextRetainedRange(scroller, selection)
      if (next !== 'keep') retainRange(next)
    }
    document.addEventListener('selectionchange', syncSelectionRetention)
    syncSelectionRetention()
    return () => {
      document.removeEventListener('selectionchange', syncSelectionRetention)
      retainRange(null)
    }
  }, [activeSlot, scrollerRef, retainRange])

  // Single scroll controller wiring: expose the virtualizer's follow API to
  // the early effects/handlers (declared above) via refs, and derive the
  // at-bottom state for the jump-to-bottom pill. The virtualizer owns slot
  // entry, streaming follow, and append-pin; ChatPage only triggers explicit
  // jumps (send, jump-to-latest pill) through these.
  const isAtBottom = virt.isAtBottom
  // Mirror the virtualizer's follow API into the refs the early effects/handlers
  // (declared above) read. Done in a layout effect rather than the render body
  // so a concurrent render React throws away can't write stale callbacks into
  // the refs. Layout effects run before passive effects, so the gating effects
  // that call vGetFollowRef.current() still see this commit's callback.
  useLayoutEffect(() => {
    vGetFollowRef.current = virt.getFollow
    vScrollerElRef.current = virt.scrollerRef?.current ?? vScrollerElRef.current
    vScrollToBottomRef.current = virt.scrollToBottom
    vFarmIsMeasuredRef.current = virt.farmIsMeasured
    earlierBarInViewRef.current = earlierBarInView
    mountIndexRef.current = virt.mountIndex
    estimateRowTopRef.current = virt.estimateRowTop
  })

  // Legacy aliases so ChatTranscriptView's JSX keeps reading the same names.
  const visibleDisplayItems = virt.virtualItems
  // A window replacement can commit after the scroll frame that requested it.
  // Re-read geometry from the committed rows so an incomplete old window cannot
  // leave its banner at rest over a different part of the transcript.
  useLayoutEffect(() => { updatePinnedPrompt() }, [visibleDisplayItems, updatePinnedPrompt])



  // A reader parked within one viewport of the absolute top while older
  // history remains is a STANDING request for more. Every edge-triggered
  // fire in this path has a death mode (measured on a pod rig — runs stalled
  // after 3/5/9/10 pages, nondeterministically): the top sentinel only fires
  // on intersection TRANSITIONS and a small landing never pushes it back out
  // of view; the window-start crossing needs a >lead→≤lead transition that a
  // top-pinned recompute skips; and a landing-edge chain races the scroll
  // compensation it reads. So this is deliberately LEVEL-triggered: a slow
  // poll, gated to near-top + hasMore + idle + no error. It cannot stack
  // requests (loadingOlder gates), cannot spin on a dead link (slotOlderError
  // gates; the scroll-retry below owns that path), and stops the moment the
  // reader leaves the top or history is exhausted.
  useEffect(() => {
    if (!slotHasMore) return
    const el = virt.scrollerRef?.current
    // The reader's own INPUT, not scroll position: a landing's compensation
    // moves scrollTop thousands of px without the reader touching anything,
    // which would otherwise end the walk after every single page — on desktop
    // (no rubber-band to hold the top) that reduced "load to the beginning"
    // to one page per manual climb. While the last fetch this poll issued is
    // newer than the last wheel/touch, the reader is still waiting on the
    // walk it started: keep going. Any input hands control back to the
    // near-top gate.
    // Both of these live in REFS, not in this effect's scope. As locals they were
    // re-created with the effect -- whose deps include `slotHasMore`, a flag that
    // history loading itself moves -- and each re-creation handed out a fresh full
    // budget plus a `lastInput` of "now", i.e. an authorization nobody gestured
    // for. A budget that a re-render can reissue is not a budget.
    // The walk needs the reader to have ACTUALLY climbed: a phone refresh
    // parks at the bottom with zero wheel/touch input ever, and boot-phase
    // transients (pre-layout scrollTop near 0, the giant-turn grouped list
    // briefly shorter than a viewport) can slip one self-issued page past
    // the near-top gate. With lastInput frozen at mount, that single
    // landing made `walking` TRUE FOREVER and the poll walked the entire
    // multi-megabyte transcript over a parked reader -- the field report
    // 'it just keeps loading previous after a refresh'. Requiring one real
    // input event this session before any poll-issued fetch turns the walk
    // back into what its own comment promises: reader-initiated.
    const noteInput = () => {
      walkLastInputAtRef.current = Date.now()
      walkPagesSinceInputRef.current = 0
      lastRealInputAtRef.current = Date.now()
      sentinelPagesSinceInputRef.current = 0
    }
    el?.addEventListener('wheel', noteInput, { passive: true })
    el?.addEventListener('touchmove', noteInput, { passive: true })
    // A wheel and a touch are not the only ways a human scrolls. PgUp/Home/space
    // and a scrollbar-thumb drag reach the top just as deliberately, and gating on
    // wheel/touchmove alone silences automatic older history for exactly the
    // readers who use them -- keyboard navigation being the accessibility path.
    // Both events are still unforgeable by us, which is the whole point of the
    // window: writing `scrollTop` fires neither, so an automatic scroll cannot
    // authorize itself. `keydown` is bound to the SCROLLER, not the document, so
    // typing in the composer is not mistaken for an intent to read history.
    el?.addEventListener('keydown', noteInput, { passive: true })
    el?.addEventListener('pointerdown', noteInput, { passive: true })
    // The walk is "in progress" when the newest older-page LANDING postdates
    // the newest user input — regardless of which trigger fired the fetch.
    // (An earlier draft keyed this on the poll's own fires and never engaged:
    // the sentinel/crossing triggers always win the race for the first page,
    // and its landing throws the reader off the near-top gate before the next
    // tick, so the poll never got the first fire it required.)
    let lastLanding = 0
    let prevLoading = false
    // Any scroll event — momentum coasting included, which fires no
    // wheel/touchmove — marks the transcript as still MOVING. A landing's
    // prepend compensation writes scrollTop, and on iOS a programmatic write
    // mid-momentum fights the fling's own curve: the reader sees the view
    // snap. Pages land only when the scroller is fully settled.
    let lastScrollEvt = 0
    const noteScroll = () => {
      lastScrollEvt = Date.now()
      // Motion kills any in-flight page: landings commit only while still.
      abortActiveOlderFetch()
    }
    el?.addEventListener('scroll', noteScroll, { passive: true })
    const t = setInterval(() => {
      const el2 = virt.scrollerRef?.current
      if (!el2 || el2.clientHeight <= 0) return
      const chat = store.getState().chat
      if (prevLoading && !chat.loadingOlder) lastLanding = Date.now()
      prevLoading = chat.loadingOlder
      const walking = lastLanding > walkLastInputAtRef.current
      // Authorization is a WINDOW, the same one the sentinel door uses -- not the
      // "has this session ever seen input" latch that used to gate this poll. The
      // latch could only ever turn ON: one flick authorized the walk for the rest
      // of the mount, `lastInput` froze at that flick, and since every landing
      // postdates it `walking` was true forever after. The page counter was then
      // the only brake, and it was reissued whenever this effect re-created. With
      // an expiring window the door is simply SHUT while nobody is scrolling,
      // which is what "reader-initiated" has to mean.
      if (!shouldContinueOlderWalk({
        sawRealInput: Date.now() - lastRealInputAtRef.current <= REAL_GESTURE_AUTH_MS,
        nearTop: el2.scrollTop <= el2.clientHeight,
        walking,
        pagesSinceInput: walkPagesSinceInputRef.current,
      })) return
      // A bottom-followed reader is reading the LIVE end: the top-of-
      // transcript walk has nothing for them, and its landings are pure
      // disturbance budget. The sentinel/crossing triggers (reader-scroll
      // driven) still page history the moment they actually climb.
      if (vGetFollowRef.current()) return
      if (!earlierBarInViewRef.current()) return
      if (Date.now() - lastScrollEvt < OLDER_LANDING_SETTLE_MS) return
      // ...and stops entirely once they are no longer climbing. The settle gate
      // above only says "not mid-gesture", so on its own it made a reader who
      // STOPPED near the top the ideal candidate: they would sit still and watch
      // several pages land under them, one prepend each, which is felt as the
      // transcript starting to move on its own a second after they let go.
      // History still reaches back as far as they like -- it loads while they
      // climb, which is when they are asking for it.
      if (Date.now() - lastScrollEvt > OLDER_WALK_ACTIVE_MS) return
      if (!chat.slotHasMore || chat.loadingOlder || chat.slotOlderError) return
      if (chat.slotCursorKey !== chat.activeSlot) return
      // Same contract as the idle prefetch: a page lands only on FULLY
      // MEASURED geometry. Without this the walk outran the farm and piled
      // unmeasured rows over the parked reader -- every measurement landing
      // was an estimate correction under their eyes (reproduced on the rig
      // as per-landing twitches at the top). Turn-grouped pages measure in
      // ~1-2s, so the walk's pace barely changes.
      const nRows = displayItemsRef.current.length
      for (let i = 0; i < nRows; i++) { if (!virt.farmIsMeasured(i)) return }
      walkPagesSinceInputRef.current += 1
      if (inspectorOn()) devLog('OLDER', `walk p${walkPagesSinceInputRef.current}`)
      void dispatch(loadOlderMessages())
    }, OLDER_TOP_POLL_MS)
    return () => {
      clearInterval(t)
      el?.removeEventListener('scroll', noteScroll)
      el?.removeEventListener('wheel', noteInput)
      el?.removeEventListener('touchmove', noteInput)
      el?.removeEventListener('keydown', noteInput)
      el?.removeEventListener('pointerdown', noteInput)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- listeners re-arm on these triggers only; handlers read refs
  }, [slotHasMore, dispatch, virt.scrollerRef])

  // The sticky in-flight spinner is only meaningful where pages LAND — at the
  // top of the loaded transcript. `loadingOlder` is now true for the whole
  // automatic walk (a dozen pages back-to-back), so gating the spinner on the
  // fetch alone kept it pinned over the reader even mid-transcript. Track
  // "near the top" cheaply: the setState is value-stable away from the
  // threshold, so mid-scroll updates bail before rendering.
  const [spinnerNearTop, setSpinnerNearTop] = useState(true)
  useEffect(() => {
    const el = virt.scrollerRef?.current
    if (!el) return
    let raf = 0
    const onScroll = () => {
      // Cancel-and-reschedule, never latch-on-pending (frameSchedulerLatch
      // guard): a dropped frame handle must not wedge the near-top signal.
      if (raf) cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        raf = 0
        setSpinnerNearTop(el.scrollTop < el.clientHeight * 1.5)
      })
    }
    onScroll()
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => { el.removeEventListener('scroll', onScroll); if (raf) cancelAnimationFrame(raf) }
  }, [virt.scrollerRef, activeSlot])

  // A failed older-page fetch PARKS pagination: the top sentinel is already
  // inside the viewport, so no new crossing ever fires and automatic paging
  // never resumes — the only way forward is the retry bar, which on a phone
  // is easy to miss, so the transcript reads as "history ends here"
  // (reproduced: fail page 3 of 7 once, scrolling never fetches again).
  // Treat a FURTHER upward scroll as retry intent — the reader is still
  // asking for older content. Cooldown-gated so a dead link costs one
  // request per gesture, not one per scroll event; the thunk's own
  // loadingOlder gate covers the in-flight window.
  const olderRetryAtRef = useRef(0)
  useEffect(() => {
    const el = virt.scrollerRef?.current
    if (!el) return
    let prevTop = el.scrollTop
    const onScroll = () => {
      const st = el.scrollTop
      const up = st < prevTop
      prevTop = st
      if (!up) return
      const chat = store.getState().chat
      if (!chat.slotOlderError || chat.loadingOlder || !chat.slotHasMore) return
      const now = Date.now()
      if (now - olderRetryAtRef.current < OLDER_RETRY_COOLDOWN_MS) return
      olderRetryAtRef.current = now
      if (inspectorOn()) devLog('OLDER', 'error-retry')
      void dispatch(loadOlderMessages())
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [dispatch, activeSlot, virt.scrollerRef])
  // No "load more" pagination indicator with virtualization — the
  // windowing engine swaps mounted/placeholder automatically.

  // Reset scroll-navigation state on slot switch.
  useEffect(() => {
    setPinned(null)
    setPinExpanded(false)
  }, [activeSlot, setPinExpanded, setPinned])

  // Stable: setRefusedPress never changes, so the steer callback is not rebuilt.
  const reportSlashRefusal = useCallback((res: { stage?: 'open' | 'turn' | 'agent'; error?: string }) => {
    setRefusedPress(slashRefusal(res))
  }, [setRefusedPress])
  return {
    handleLoadEarlier, virt, isAtBottom, visibleDisplayItems, spinnerNearTop, reportSlashRefusal,
  }
}
