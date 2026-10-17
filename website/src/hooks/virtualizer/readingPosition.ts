// The persisted reading position of the chat virtualizer (see ScrollAnchorCache).
//
// A session remembers the row the reader was reading and where on screen it
// sat, and re-enters there instead of at the live end. This module owns that
// lifecycle: the latch taken when a session is ENTERED (first mount, slot
// switch, a disturbed visibility return), the debounced save as the reader's
// own scrolling settles, the leave flush on a switch, and the restore that
// re-lands the anchored row -- plus its settle loop -- when the row arrives.
// While a restore owns the position the automatic pins stand down
// (restoreOwnsPosition, settleGateRef). The shift compensations stand down only
// while the settle loop is actively measuring its row (settleMeasuringRef; see
// shiftCompensationAllowed for why the rule must not read "a restore is in
// flight").

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MutableRefObject, type RefObject } from 'react'
import {
  loadScrollAnchor,
  saveScrollAnchor,
  clearScrollAnchor,
  anchorWriteChangesState,
  type ScrollAnchor,
} from './ScrollAnchorCache'
import { captureTopAnchorFrom } from './anchorGeometry'
import { anchorSettleConverged, computeAtBottom, isSelfScroll, SCROLL_SETTLE_MS, scrollerCollapsed } from './FollowController'
import { computeJumpWindow, getOffset as getOffsetFn, initialWindow, tailWindow, type HeightGetter, type WindowRange } from './WindowCalculator'
import type { HeightIndex } from './HeightIndex'
import type { FollowState, Pinning } from './followPolicy'
import type { ShiftCapture } from './shiftCompensation'
import { devLog, devWatchScroller, inspectorOn, keyShape, shortId } from '../../dev/scrollInspector'

type Ref<V> = MutableRefObject<V>
type SetWindowRange = (next: WindowRange | ((prev: WindowRange) => WindowRange)) => void

/** How long an anchored entry may hold its caller's skeleton waiting for the
 *  anchored row to hydrate. A transcript arrives in CHUNKS, not at once: the
 *  entry commit was measured carrying 6 rows and the next one 17, so a row that
 *  is absent right now is not a row that is gone. Consuming the anchor on that
 *  first commit is what made the restore miss EVERY time (idx=-1 with the row
 *  arriving milliseconds later), and the miss then cleared the anchor on its way
 *  out, so nothing was left to try again with.
 *
 *  Expiring is a visible, safe fallback rather than a stuck state: the skeleton
 *  lifts and the transcript opens at the live end, which is where an entry with
 *  no usable anchor belongs anyway. Sized to outlast a hydration gap measured in
 *  one commit, not to wait out a network fetch. */
const RESTORE_HYDRATE_WAIT_MS = 1200

// Reading-position anchor persistence (see ScrollAnchorCache). The anchor is
// captured on scroll-SETTLE, not per scroll event: captureTopAnchor reads a
// getBoundingClientRect per mounted row, which is fine once per pause but not
// at scroll-event rate. Trailing-edge, non-resetting timer: it fires at most
// once per window even during a continuous scroll/stream, so "returned to the
// bottom" reliably clears the anchor instead of being starved by resets.
const ANCHOR_SAVE_DEBOUNCE_MS = 200
/** A saved reading anchor must trace back to the USER's own scrolling. A
 *  self-inflicted displacement (a mis-clamped pin, native anchoring against a
 *  resizing neighbor) fires the same scroll events as a person and would
 *  persist the displaced position -- the next reload then restores it and the
 *  session "opens mid-transcript" with the displacement laundered into
 *  intent. Saving is therefore gated on HARD input (wheel / touch / scrollbar
 *  grab / scrolling keys -- attachUserScrollIntent's event set, plus a real
 *  grab that interrupts a smooth pin) within this window. CLEARING at the
 *  bottom stays unconditional: clearing only ever restores the default
 *  land-at-bottom, which is always safe. `lastUserScrollAtRef` is NOT usable
 *  here: the scroll handler stamps it for any non-clamp scroll event,
 *  including the browser's native-anchoring adjustments. */
const ANCHOR_SAVE_INTENT_WINDOW_MS = 3000

/** How long the restore keeps re-landing the anchored row against its LIVE DOM
 *  position while measurements arrive.
 *
 *  A frame COUNT was the wrong unit. The initial write is offset math over a
 *  height index that still prices unmeasured rows at the estimate, so the
 *  correction has to outlast real measurement -- and on this transcript a single
 *  row averages 660-800px with syntax-highlighted code blocks costing ~90ms of
 *  main thread each, so three frames (~50ms) expired before the heights above
 *  the anchor had resolved at all. Two returns to the same session with the same
 *  anchor and the same row index then landed 184px apart, because the only thing
 *  that differed was how much of the transcript above had been measured yet.
 *
 *  A time budget is still bounded, and the loop aborts on a genuine user scroll,
 *  so a longer window cannot fight a reader who takes over. */
const ANCHOR_RESTORE_SETTLE_MS = 600

/** Smallest anchor-position error the settle loop will act on.
 *
 *  `scrollTop` lands on the device-pixel grid (dpr 3 on the reporter's phone)
 *  while `getBoundingClientRect` reports CSS pixels, so a sub-pixel residual
 *  reads as a whole pixel of error that writing cannot remove. At a 0.5px
 *  threshold the loop measured +1px and wrote +1px on 34 CONSECUTIVE frames
 *  without converging -- a 600ms wobble, not a correction. The threshold has to
 *  sit above what the two coordinate systems can disagree about while staying
 *  far below the errors this exists to fix (measured: 111px on one frame). */
const ANCHOR_SETTLE_TOLERANCE_PX = 1.5

export interface ReadingPositionEntry<T = unknown> {
  pendingRestoreRef: Ref<ScrollAnchor | null | undefined>
  returnRestoreRef: Ref<boolean>
  restoreDeadlineRef: Ref<number>
  restoreLastCountRef: Ref<number>
  restoreTimerRef: Ref<ReturnType<typeof setTimeout> | null>
  slotPinDoneRef: Ref<string | null>
  settleRafRef: Ref<number>
  settleMeasuringRef: Ref<boolean>
  settleGateRef: Ref<boolean>
  restoreOwnsPosition: () => boolean
  /** When the current session was last ENTERED (latch time; see readerMovedSinceEntry). */
  entryAtRef: Ref<number>
  /** The reader MOVED the scroller after the entry latch: they picked a position. */
  readerMovedSinceEntry: () => boolean
  restoreEval: number
  setRestoreEval: (update: (n: number) => number) => void
  anchorSaveTimerRef: Ref<ReturnType<typeof setTimeout> | null>
  /** Identity context of the last scroll burst (see the session transition). */
  lastScrollCtxRef: Ref<{ session: string; items: readonly T[]; getKey: (it: T, i: number) => string } | null>
  sessionIdRef: Ref<string>
  captureTopAnchor: () => ScrollAnchor | null
  scheduleAnchorSave: (flushPending?: boolean) => void
  /** The caller's skeleton gate, read at the end of render. */
  restoreGateNow: () => boolean
  /** Drop a pending debounced save (the unmount teardown). */
  dropPendingAnchorSave: () => void
}

export function useReadingPositionEntry<T>(ctx: {
  sessionId: string
  itemCount: number
  overscan: number
  initialPlacement: 'top' | 'bottom'
  followOutput: boolean
  bottomThreshold: number
  scrollerRef: RefObject<HTMLDivElement | null>
  elIndexRef: Ref<Map<Element, number>>
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  getStableIdRef: Ref<((item: T, index: number) => string) | undefined>
  getAltIdRef: Ref<((item: T, index: number) => string) | undefined>
  altIdAtIndex: (idx: number) => string | null
  setWindowRange: SetWindowRange
  setIsAtBottom: (next: boolean) => void
  follow: Pick<FollowState, 'stickRef' | 'lastWriteTopRef' | 'lastWriteClientHRef' | 'lastHardInputAtRef' | 'lastProgrammaticTopRef' | 'lastDirectionalInputAtRef'>
}): ReadingPositionEntry<T> {
  const {
    sessionId, itemCount, overscan, initialPlacement, followOutput, bottomThreshold,
    scrollerRef, elIndexRef, itemsRef, getKeyRef, getStableIdRef, getAltIdRef, altIdAtIndex,
    setWindowRange, setIsAtBottom,
  } = ctx
  const { stickRef, lastWriteTopRef, lastWriteClientHRef, lastHardInputAtRef, lastProgrammaticTopRef, lastDirectionalInputAtRef } = ctx.follow

  // ---- Reading-position anchor (persisted; see ScrollAnchorCache) ----
  //
  // `pendingRestoreRef` latches the saved anchor for the CURRENT session the
  // moment the session is entered (first mount or slot switch), BEFORE any
  // pin can fire. Latching is what makes the restore immune to the entry
  // pin's own scroll events: a bottom pin marks the session "at bottom",
  // whose debounced save would clear the very anchor being restored.
  // `undefined` means "not yet latched for this session" (first render).
  const pendingRestoreRef = useRef<ScrollAnchor | null | undefined>(undefined)
  // True while the pending restore is a visibility-RETURN re-placement rather
  // than a slot ENTRY. `restoreGate` hides the transcript behind the caller's
  // skeleton while an entry restore waits for its row, because the rows under
  // it are a partial, unpositioned transcript. On a return the rows are already
  // mounted and positioned -- only scrollTop moves -- so the gate must stay
  // down or the reader comes back to a blanked transcript until settle.
  const returnRestoreRef = useRef(false)
  // Wall-clock ceiling for the pending restore of the CURRENT session.
  const restoreDeadlineRef = useRef<number>(0)
  // Highest item count seen while a restore is pending; growth past it renews the wait.
  const restoreLastCountRef = useRef(-1)
  const restoreTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Guards the once-per-slot-entry bottom pin (see the slot-entry layout effect
  // below). Declared here rather than beside that effect because the
  // visibility-return handler (also below, but earlier) resets it to re-arm
  // placement after a disturbed hidden interval.
  const slotPinDoneRef = useRef<string | null>(null)
  // The restore's settle loop, owned OUTSIDE any one commit. Its whole job is to
  // re-land the anchored row as measurements arrive, and those arrive across
  // commits -- so tying its lifetime to the effect's cleanup meant the very next
  // render cancelled it. It aborts itself on a session change or a real user
  // scroll, which is what actually bounds it.
  const settleRafRef = useRef(0)
  /** True only while the settle loop is measuring its anchor row and correcting
   *  against it. A settle that cannot find the row holds its gate but corrects
   *  nothing, and must not keep the other compensations standing down. */
  const settleMeasuringRef = useRef(false)
  // True from the restore's positioning write until the settle loop first agrees
  // with the anchor. The caller's cover must span BOTH: the initial write is
  // offset math over estimated heights (measured 1035px off on one entry), so
  // lifting the cover when the anchor merely RESOLVES shows the reader the wrong
  // position and then the jump that corrects it -- the one jump the cover exists
  // to hide. Cleared on convergence rather than at the budget's end so a switch
  // is not gated on a fixed 600ms.
  const settleGateRef = useRef(false)
  /** Whether an anchor restore currently owns the scroll position.
   *
   *  `settleGate` is up while a restore is landing and re-landing; `pendingRestore`
   *  means one is OWED but has not run yet (hydration still arriving). Both must
   *  refuse an automatic bottom pin: the second because the session-entry contract
   *  already starts follow RELEASED when an anchor is pending, so pinning would
   *  contradict the decision that was made when the session was entered.
   *
   *  Every write to the PERSISTED anchor asks this too -- the debounced save and the
   *  leave flush -- and must ask it here rather than reading `pendingRestore` alone.
   *  That ref goes false as soon as the anchored row is located, which is before the
   *  scroller has been written and long before the settle stops correcting; a site
   *  gated on it treats our own landing as the reader's chosen position. The gesture
   *  revocation in restoreAnchor is not a substitute, because the at-bottom CLEAR
   *  runs above the intent gate.
   *
   *  Declared beside the two refs it reads, ahead of every caller: the leave flush is
   *  the earliest of them, and a caller that cannot reach this predicate reaches for
   *  `pendingRestore` instead, which is the defect above. */
  // When the current session was entered (the latch below). A reader who MOVES
  // the scroller after it has chosen a position while a restore was still only
  // OWED; landing that restore later snaps them (paged older history, went back
  // to the bottom) onto the old row (#11625). A hard-input stamp alone is not a
  // move: a wheel at the end or a tap scrolls nothing, and must not cost the
  // reader their saved position. So the move is recorded by the scroll path
  // (scheduleAnchorSave): scrollTop changed within the settle window of a
  // DIRECTIONAL input stamped after entry (a tap, a scrollbar grab or a
  // zero-delta wheel names no direction, so the browser's own anchoring shift
  // after one is not the reader), and not to a target WE wrote (a prepend
  // compensation lands here too). Checked against lastProgrammaticTopRef, not
  // lastWriteTopRef: the follow handler runs first and re-baselines the latter
  // to a reader's own arrival at the end.
  const entryAtRef = useRef(0)
  const readerMovedAtRef = useRef(Number.NEGATIVE_INFINITY)
  const lastSeenTopRef = useRef(-1)
  const readerMovedSinceEntry = useCallback(
    (): boolean => readerMovedAtRef.current > entryAtRef.current,
    [],
  )
  const restoreOwnsPosition = useCallback(
    (): boolean => settleGateRef.current || (pendingRestoreRef.current !== null && !readerMovedSinceEntry()),
    [readerMovedSinceEntry],
  )
  // Bumped whenever the pending restore RESOLVES (applied or expired) so the
  // gate below is never read stale, and by the expiry timer so the effect gets
  // one final evaluation when hydration stops before the row shows up.
  const [restoreEval, setRestoreEval] = useState(0)
  // Debounced-save bookkeeping: one trailing, NON-resetting timer, plus the
  // last state actually written per session so streaming (which fires the
  // timer repeatedly while pinned to the bottom) doesn't spam localStorage.
  const anchorSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** The last anchor actually written per session; `anchor: null` means CLEARED.
   *
   *  Holds the anchor rather than a formatted key so the question "would this write
   *  change anything" is asked through `anchorWriteChangesState` -- the same predicate
   *  the storage layer applies. A second spelling here silently outranked it: a
   *  `key@top` string omits `alt`, so a row whose LEAD id changed (an older-page
   *  prepend regroups it, leaving tail and offset untouched) never reached storage at
   *  all. The stored `alt` then went stale, and a later append renamed the tail --
   *  leaving BOTH identities unresolvable, which is exactly what carrying two of them
   *  is supposed to prevent. */
  const anchorSavedStateRef = useRef<{ session: string; anchor: ScrollAnchor | null } | null>(null)
  // Identity context of the last scroll burst: which session it belonged to
  // and how to resolve row keys for it. Reference-only (no rect reads), set on
  // every scroll event. The slot-switch flush below needs it because during
  // the switch RENDER, itemsRef/getKeyRef may already hold the INCOMING
  // session's data while the DOM (elIndexRef nodes, scroller geometry) still
  // shows the outgoing one — resolving keys through the live refs there would
  // save the wrong keys under the old session id.
  const lastScrollCtxRef = useRef<{
    session: string
    items: readonly T[]
    getKey: (it: T, i: number) => string
  } | null>(null)
  if (pendingRestoreRef.current === undefined) {
    // First render: latch any saved anchor for the initial session. Reading
    // localStorage during render matches the HeightCache constructor in the
    // height owner, which runs later in this same render.
    pendingRestoreRef.current = loadScrollAnchor(sessionId)
    returnRestoreRef.current = false
    entryAtRef.current = performance.now()
    if (pendingRestoreRef.current) {
      stickRef.current = false
      restoreDeadlineRef.current = performance.now() + RESTORE_HYDRATE_WAIT_MS
    }
  }

  // Reset window + follow state to the tail/bottom when the session changes.
  // useState's lazy initializer only runs on first mount, so without this the
  // second visit to a slot would carry over the last window/stick state,
  // defeating the "open at bottom" contract (and causing the "lands in the
  // middle" bug). Render-time sentinel pattern (mirrors the HeightCache reset
  // in the height owner); React permits state updates during render when guarded by a
  // "props changed" check. lastWriteTopRef is reset to -1 so the leftover
  // scrollTop from the previous session is not mistaken for a user scroll-up.
  const sessionIdRef = useRef<string>(sessionId)
  if (sessionIdRef.current !== sessionId) {
    const prevSession = sessionIdRef.current
    sessionIdRef.current = sessionId
    setWindowRange(initialWindow(itemCount, overscan, initialPlacement))
    lastWriteTopRef.current = -1
    lastWriteClientHRef.current = -1
    setIsAtBottom(true)
    // A pending debounced save belongs to the OUTGOING session: flush it NOW,
    // synchronously, instead of dropping it — a scroll-then-switch inside the
    // debounce window must not lose the newest reading position. This render
    // has not committed, so the DOM still shows the outgoing session
    // (elIndexRef nodes, scroller geometry), and lastScrollCtxRef resolves
    // row keys against ITS items — the live itemsRef may already hold the
    // incoming session's data here. Once-per-switch rect reads over the
    // mounted window (~2×overscan rows) — negligible. Skipped while a restore
    // for the outgoing session was still pending (transitional geometry).
    devLog('LEAVE', `${shortId(prevSession)} pendingTimer=${anchorSaveTimerRef.current !== null ? 1 : 0}`)
    if (anchorSaveTimerRef.current !== null) {
      clearTimeout(anchorSaveTimerRef.current)
      anchorSaveTimerRef.current = null
      const ctx = lastScrollCtxRef.current
      const el = scrollerRef.current
      if (!(ctx && ctx.session === prevSession && el && !restoreOwnsPosition())) {
        devLog('LEAVE.skip', `ctx=${ctx ? 1 : 0} same=${ctx && ctx.session === prevSession ? 1 : 0} el=${el ? 1 : 0} own=${restoreOwnsPosition() ? 1 : 0}`)
      }
      if (ctx && ctx.session === prevSession && el && !restoreOwnsPosition()) {
        const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
        // `stick` is the AUTHORITATIVE bottom truth here: while follow is
        // engaged the reader IS at the bottom semantically, even when the pin
        // trails the last streamed growth by a frame -- exactly the instant a
        // switch tends to interrupt. Trusting instantaneous geometry alone
        // persisted that transient as a reading anchor; switching back then
        // restored it, yanking the reader off a bottom they never left.
        devLog('LEAVE.flush', `stick=${stickRef.current ? 1 : 0} bot=${computeAtBottom(geom, bottomThreshold) ? 1 : 0} dist=${Math.round(geom.scrollHeight - geom.clientHeight - geom.scrollTop)}`)
        if (stickRef.current || computeAtBottom(geom, bottomThreshold)) {
          clearScrollAnchor(prevSession)
        } else {
          const a = captureTopAnchorFrom(el, elIndexRef.current.entries(), (idx) => {
            const it = ctx.items[idx]
            if (!it) return null
            // The stable id is a pure function of the ITEM, so the live fn is
            // correct against the outgoing commit's items; `ctx.getKey` is not
            // (it prices against that render's rowKeys). Same vocabulary
            // findAnchorIndex resolves in -- see its comment.
            const idFn = getStableIdRef.current
            return idFn ? idFn(it, idx) : ctx.getKey(it, idx)
          })
          // The capture's `index` is the outgoing commit's and means nothing
          // after a reload; persist the key/top pair only.
          // Both identities, for the reason ScrollAnchor.alt gives: the leave
          // path is exactly where a live turn is abandoned mid-growth.
          if (a) {
            const leadFn = getAltIdRef.current
            const leadIt = ctx.items[a.index]
            const alt = leadFn && leadIt ? leadFn(leadIt, a.index) : null
            saveScrollAnchor(prevSession, alt ? { key: a.key, top: a.top, alt } : { key: a.key, top: a.top })
          }
        }
      }
    }
    lastScrollCtxRef.current = null
    // Latch the entered session's saved reading position (if any). With an
    // anchor pending, follow starts RELEASED so the bulk-hydration path below
    // doesn't tail-pin before the restore runs; without one, the default
    // open-at-bottom contract stands.
    pendingRestoreRef.current = loadScrollAnchor(sessionId)
    returnRestoreRef.current = false
    entryAtRef.current = performance.now()
    stickRef.current = pendingRestoreRef.current ? false : followOutput
    if (restoreTimerRef.current !== null) {
      clearTimeout(restoreTimerRef.current)
      restoreTimerRef.current = null
    }
    restoreLastCountRef.current = -1
    restoreDeadlineRef.current = pendingRestoreRef.current
      ? performance.now() + RESTORE_HYDRATE_WAIT_MS
      : 0
  }

  // Topmost visible mounted row, resolved against the LIVE items. Used by the
  // scroll-anchor preservation path and the debounced reading-position save.
  // (The slot-switch flush calls captureTopAnchorFrom directly with a
  // snapshot resolver instead — see the session sentinel.)
  const captureTopAnchor = useCallback((): ScrollAnchor | null => {
    const el = scrollerRef.current
    if (!el) return null
    const a = captureTopAnchorFrom(el, elIndexRef.current.entries(), (idx) => {
      const it = itemsRef.current[idx]
      if (!it) return null
      // Same vocabulary findAnchorIndex resolves in -- see its comment.
      const idFn = getStableIdRef.current
      return idFn ? idFn(it, idx) : getKeyRef.current(it, idx)
    })
    if (!a) return null
    // Carry the row's OTHER identity too: the two ends fail in opposite cases
    // (see ScrollAnchor.alt), and a switch into a live turn triggers both.
    const alt = altIdAtIndex(a.index)
    return alt ? { key: a.key, top: a.top, alt } : { key: a.key, top: a.top }
  }, [scrollerRef, altIdAtIndex, elIndexRef, itemsRef, getStableIdRef, getKeyRef])

  // ---- Reading-position anchor: debounced save on scroll settle ----
  //
  // Fired from the passive scroll listener. At settle time (not per event —
  // captureTopAnchor reads a rect per mounted row) the live geometry decides:
  //   - at the bottom → the anchor must be ABSENT ("no anchor" is what makes
  //     the next slot entry take the default pin-to-bottom path), so clear it;
  //   - scrolled up → persist the topmost visible row's key + viewport offset.
  // Self-scrolls schedule saves too, deliberately: a programmatic jump/pin
  // still changes the truth being persisted. The fire-time session guard
  // covers a timer surviving into a slot switch.
  //
  // `flushPending` runs the body SYNCHRONOUSLY instead of arming the timer --
  // only when a save is already pending, and in its place. The visibility-hide
  // branch below uses it: the timer would otherwise fire against a box the
  // browser has already collapsed, or not at all before the reader returns, and
  // the return would then read a stale anchor. Same predicates either way --
  // the flush writes exactly what the timer was about to.
  const scheduleAnchorSave = useCallback((flushPending = false) => {
    const live = scrollerRef.current
    if (live && !flushPending) {
      const top = live.scrollTop
      const now = performance.now()
      const hardAt = lastDirectionalInputAtRef.current
      if (
        lastSeenTopRef.current >= 0 && top !== lastSeenTopRef.current &&
        hardAt > entryAtRef.current && now - hardAt <= SCROLL_SETTLE_MS &&
        !isSelfScroll(top, lastProgrammaticTopRef.current)
      ) {
        readerMovedAtRef.current = now
      }
      lastSeenTopRef.current = top
    }
    if (flushPending) {
      if (anchorSaveTimerRef.current === null) return
      clearTimeout(anchorSaveTimerRef.current)
      anchorSaveTimerRef.current = null
    } else if (anchorSaveTimerRef.current !== null) {
      return
    }
    const scheduledSession = sessionIdRef.current
    const run = () => {
      if (sessionIdRef.current !== scheduledSession) return
      // A restore OWNS the position until its settle has finished converging, and
      // `pendingRestore` alone does not say that: it is cleared the moment the anchored
      // row is found, BEFORE restoreAnchor writes the scroller and before the settle
      // re-lands it. In that gap the geometry is still ours, not the reader's -- and the
      // at-bottom branch below writes to storage ABOVE the intent gate, so revoking the
      // gesture (which restoreAnchor does for exactly this reason) does not reach it. A
      // restore that clamps at or near the end therefore CLEARED the anchor it had just
      // restored, and the next entry, finding none, opened at the bottom.
      if (restoreOwnsPosition()) return
      const el = scrollerRef.current
      if (!el) return
      const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
      const saved = anchorSavedStateRef.current
      if (computeAtBottom(geom, bottomThreshold)) {
        devLog('DEB.atBottom', `${shortId(scheduledSession)} -> clear`)
        if (saved?.session !== scheduledSession || saved.anchor !== null) {
          clearScrollAnchor(scheduledSession)
          anchorSavedStateRef.current = { session: scheduledSession, anchor: null }
        }
        return
      }
      // Save only positions the user put themselves at (see the constant's
      // doc). Self-scroll displacements must never become the restore target.
      const sinceHard = performance.now() - lastHardInputAtRef.current
      if (sinceHard > ANCHOR_SAVE_INTENT_WINDOW_MS) {
        devLog('DEB.noIntent', `${shortId(scheduledSession)} sinceHard=${Number.isFinite(sinceHard) ? Math.round(sinceHard) : 'never'}ms`)
        return
      }
      const a = captureTopAnchor()
      if (!a) return
      if (
        saved?.session === scheduledSession &&
        saved.anchor !== null &&
        !anchorWriteChangesState(saved.anchor, a)
      ) {
        return
      }
      saveScrollAnchor(scheduledSession, a)
      anchorSavedStateRef.current = { session: scheduledSession, anchor: a }
    }
    if (flushPending) {
      run()
      return
    }
    anchorSaveTimerRef.current = setTimeout(() => {
      anchorSaveTimerRef.current = null
      run()
    }, ANCHOR_SAVE_DEBOUNCE_MS)
  }, [lastHardInputAtRef, lastProgrammaticTopRef, lastDirectionalInputAtRef, bottomThreshold, scrollerRef, captureTopAnchor, restoreOwnsPosition])

  const restoreGateNow = useCallback(
    (): boolean => !returnRestoreRef.current && (pendingRestoreRef.current != null || settleGateRef.current),
    [],
  )

  const dropPendingAnchorSave = useCallback(() => {
    // Drop (not flush) a pending anchor save: at unmount time the rows'
    // layout is no longer trustworthy, and the last settled save already
    // captured the position the user actually read at.
    if (anchorSaveTimerRef.current) {
      clearTimeout(anchorSaveTimerRef.current)
      anchorSaveTimerRef.current = null
    }
  }, [])

  return {
    pendingRestoreRef,
    returnRestoreRef,
    restoreDeadlineRef,
    restoreLastCountRef,
    restoreTimerRef,
    slotPinDoneRef,
    settleRafRef,
    settleMeasuringRef,
    settleGateRef,
    restoreOwnsPosition,
    entryAtRef,
    readerMovedSinceEntry,
    restoreEval,
    setRestoreEval,
    anchorSaveTimerRef,
    lastScrollCtxRef,
    sessionIdRef,
    captureTopAnchor,
    scheduleAnchorSave,
    restoreGateNow,
    dropPendingAnchorSave,
  }
}

export function useVisibilityReplacement<T>(ctx: {
  sessionId: string
  followOutput: boolean
  bottomThreshold: number
  overscan: number
  scrollerRef: RefObject<HTMLDivElement | null>
  itemsRef: Ref<T[]>
  setWindowRange: SetWindowRange
  reading: ReadingPositionEntry<T>
  follow: Pick<FollowState, 'stickRef' | 'lastWriteTopRef' | 'lastWriteClientHRef'>
  pinning: Pick<Pinning, 'forcePin'>
}): void {
  const { sessionId, followOutput, bottomThreshold, overscan, scrollerRef, itemsRef, setWindowRange } = ctx
  const {
    pendingRestoreRef, returnRestoreRef, restoreDeadlineRef, restoreLastCountRef, restoreTimerRef, slotPinDoneRef,
    restoreOwnsPosition, setRestoreEval, sessionIdRef, captureTopAnchor, scheduleAnchorSave, entryAtRef,
  } = ctx.reading
  const { stickRef, lastWriteTopRef, lastWriteClientHRef } = ctx.follow
  const { forcePin } = ctx.pinning

  // ---- Visibility: snapshot on hide, re-place on a DISTURBED return ----
  //
  // The slot-entry placement (restore the saved anchor, else force-pin to the
  // bottom) runs only on a session change or first mount. A mobile tab return
  // keeps the SAME mounted sessionId, so none of it re-runs -- yet while the
  // tab was hidden the scroller got a zero-height layout, which released
  // `stick`, and the WebSocket heal path rebuilt the rows under the still-
  // mounted scroller. WebKit has no scroll anchoring to absorb that, so the
  // reader landed far back instead of at the live end (kirodotdev/KiroCrew: the
  // "transcript jumps back on tab return" mobile report).
  //
  // Re-placing on EVERY return would be its own regression: a desktop reader
  // who jumped to a search hit (a position the intent gate deliberately never
  // persists) and briefly switched tabs would come back at the live end. So the
  // return is gated on evidence that the hidden interval MOVED something:
  //
  //   HIDE (visible -> hidden), while the layout is still intact: flush the
  //   pending debounced anchor save (what the timer was about to write, written
  //   before the box collapses), then snapshot `stick`, the scroll geometry and
  //   -- for a released reader -- the top visible row as an in-memory anchor.
  //   That anchor is the return's target: it holds positions the persisted
  //   anchor never will (programmatic jumps), and it is taken from the live box.
  //
  //   RETURN (hidden -> visible): compare the live scroller against the
  //   snapshot. Disturbed means follow was released while hidden, a follower is
  //   off the live end (pins are skipped in a collapsed box), or
  //   scrollTop / clientHeight differ from the snapshot (a clamp or rebuild
  //   under the reader). An undisturbed return does nothing at all -- no
  //   latch, no write, no re-render -- which is the desktop tab switch.
  //
  //   Disturbed + following at hide: force-pin now (the box is back) and
  //   re-arm the slot-entry pin so the post-return commit places against the
  //   real height. Disturbed + released at hide: latch the hide-time anchor
  //   (falling back to the persisted one, then to the default pin) exactly as
  //   a slot entry would, so the restore owns the position.
  //
  // A restore already owning the position (pending or settling) is left alone
  // on both ends: its own settle loop lands it against the returned layout.
  //
  // Sequencing: the released path only re-arms the latches and bumps
  // `restoreEval`; the scrollTop write happens in the slot-entry
  // useLayoutEffect, which runs AFTER the commit once rows are measured and
  // re-runs on every hydration commit -- the same measured-rows mechanism the
  // session switch uses, not a bare timeout.
  const hideSnapshotRef = useRef<{
    session: string
    stick: boolean
    scrollTop: number
    clientHeight: number
    anchor: ScrollAnchor | null
  } | null>(null)
  useEffect(() => {
    if (typeof document === 'undefined') return
    let wasHidden = document.hidden
    const onHide = () => {
      const el = scrollerRef.current
      if (!el) {
        hideSnapshotRef.current = null
        return
      }
      // Mirror the slot-switch LEAVE flush: a save still inside its debounce
      // window is written now, against the intact box, with the same
      // predicates the timer would have applied.
      scheduleAnchorSave(true)
      const stick = stickRef.current
      hideSnapshotRef.current = {
        session: sessionIdRef.current,
        stick,
        scrollTop: el.scrollTop,
        clientHeight: el.clientHeight,
        // A follower's position is the bottom; a restore's position is the
        // restore's. Only a released reader has a row worth remembering.
        anchor: stick || restoreOwnsPosition() ? null : captureTopAnchor(),
      }
    }
    const onReturn = () => {
      const snap = hideSnapshotRef.current
      hideSnapshotRef.current = null
      // No snapshot (mounted while hidden, no scroller at hide) or one taken
      // for another session: nothing to compare against, so nothing to do.
      // Compared against the LIVE ref, not the effect's captured `sessionId`:
      // the ref flips during render on a session switch, and a switch that
      // lands while hidden leaves this listener closed over the outgoing id
      // until React re-subscribes it -- a window a throttled background tab
      // stretches. Reading the ref means a stale listener can never accept the
      // prior session's snapshot for the transcript now on screen.
      const liveSession = sessionIdRef.current
      if (!snap || snap.session !== liveSession) return
      const el = scrollerRef.current
      if (!el) return
      if (restoreOwnsPosition()) return
      const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
      const followReleased = snap.stick && !stickRef.current
      const followerOffBottom = snap.stick && stickRef.current && !computeAtBottom(geom, bottomThreshold)
      const positionMoved = geom.scrollTop !== snap.scrollTop
      const boxChanged = geom.clientHeight !== snap.clientHeight
      if (!(followReleased || followerOffBottom || positionMoved || boxChanged)) return
      // Re-arm slot-entry placement for the CURRENT session, mirroring the
      // session-switch render block. Reset lastWrite* so the freshly laid-out
      // height is not compared against a stale write recorded while the tab was
      // hidden (which would read as a user scroll-up).
      slotPinDoneRef.current = null
      lastWriteTopRef.current = -1
      lastWriteClientHRef.current = -1
      if (restoreTimerRef.current !== null) {
        clearTimeout(restoreTimerRef.current)
        restoreTimerRef.current = null
      }
      restoreLastCountRef.current = -1
      if (snap.stick) {
        // Following at hide: the live end is the position. Remount the TAIL
        // window first (the mounted window may still be mid-history from the
        // hidden-interval release), so the pin lands the live turn rather than
        // the bottom spacer with rows still to mount -- the same order the
        // bulk-hydration pin uses. Then pin before the first visible frame when
        // the box is already back; the re-armed slot-entry effect repeats the
        // pin against the post-return commit.
        pendingRestoreRef.current = null
        returnRestoreRef.current = false
        restoreDeadlineRef.current = 0
        stickRef.current = followOutput
        const count = itemsRef.current.length
        setWindowRange(tailWindow(count, overscan))
        if (!scrollerCollapsed(el)) forcePin()
      } else {
        // Released at hide: the hide-time row is the position. The persisted
        // anchor is only a fallback -- it can be older than the position the
        // reader was actually at.
        const target = snap.anchor ?? loadScrollAnchor(liveSession)
        pendingRestoreRef.current = target
        returnRestoreRef.current = target != null
        entryAtRef.current = performance.now()
        stickRef.current = target ? false : followOutput
        restoreDeadlineRef.current = target ? performance.now() + RESTORE_HYDRATE_WAIT_MS : 0
      }
      // Re-enter the slot-entry layout effect (its restoreEval dep) so it
      // re-places once the post-return rows are committed and measured.
      setRestoreEval((n) => n + 1)
    }
    const onVisibility = () => {
      const nowHidden = document.hidden
      const changed = wasHidden !== nowHidden
      wasHidden = nowHidden
      if (!changed) return
      if (nowHidden) onHide()
      else onReturn()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [
    sessionId, followOutput, bottomThreshold, overscan, scrollerRef, captureTopAnchor, restoreOwnsPosition, scheduleAnchorSave, forcePin,
    pendingRestoreRef, returnRestoreRef, restoreDeadlineRef, restoreLastCountRef, restoreTimerRef, slotPinDoneRef,
    setRestoreEval, sessionIdRef, stickRef, lastWriteTopRef, lastWriteClientHRef, itemsRef, setWindowRange, entryAtRef,
  ])
}

export function useReadingPositionRestore<T>(ctx: {
  sessionId: string
  scrollerEl: HTMLDivElement | null
  itemCount: number
  overscan: number
  initialPlacement: 'top' | 'bottom'
  followOutput: boolean
  scrollerRef: RefObject<HTMLDivElement | null>
  elIndexRef: Ref<Map<Element, number>>
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  getStableIdRef: Ref<((item: T, index: number) => string) | undefined>
  heightIndexRef: Ref<HeightIndex | null>
  getH: HeightGetter
  findAnchorIndex: (anchor: ScrollAnchor) => number
  anchoredRowIdentity: (index: number, anchor: ScrollAnchor) => { rowId: string | null; matches: boolean }
  setWindowRange: SetWindowRange
  setIsAtBottom: (next: boolean) => void
  reading: ReadingPositionEntry<T>
  shift: Pick<ShiftCapture, 'dropShiftCapture'>
  follow: Pick<FollowState, 'stickRef' | 'lastWriteTopRef' | 'lastWriteClientHRef' | 'lastHardInputAtRef' | 'writeScrollTop'>
  pinning: Pick<Pinning, 'forcePin'>
}): void {
  const {
    sessionId, scrollerEl, itemCount, overscan, initialPlacement, followOutput, scrollerRef, elIndexRef,
    itemsRef, getKeyRef, getStableIdRef, heightIndexRef, getH, findAnchorIndex, anchoredRowIdentity,
    setWindowRange, setIsAtBottom,
  } = ctx
  const {
    pendingRestoreRef, returnRestoreRef, restoreDeadlineRef, restoreLastCountRef, restoreTimerRef, slotPinDoneRef,
    settleRafRef, settleMeasuringRef, settleGateRef, restoreEval, setRestoreEval, sessionIdRef,
    readerMovedSinceEntry, scheduleAnchorSave,
  } = ctx.reading
  const { dropShiftCapture } = ctx.shift
  const { stickRef, lastWriteTopRef, lastWriteClientHRef, lastHardInputAtRef, writeScrollTop } = ctx.follow
  const { forcePin } = ctx.pinning

  // ---- Reading-position restore (see ScrollAnchorCache) ----

  useEffect(() => () => {
    if (restoreTimerRef.current !== null) clearTimeout(restoreTimerRef.current)
    if (settleRafRef.current) cancelAnimationFrame(settleRafRef.current)
  }, [restoreTimerRef, settleRafRef])

  // Restore a saved reading position: mount a window around the anchored row
  // and place it back at the saved viewport offset — instead of the slot-entry
  // bottom pin. Positioning is anchored to the ROW, not a raw scrollTop: a raw
  // pixel offset is meaningless before rows are measured (the historical
  // "lands in the middle" bug), while the row's content offset is exact once
  // its window commits, warm from the persisted HeightCache on a revisit, and
  // corrected against live DOM geometry by the settle frames below.
  //
  // Follow stays RELEASED (the restore is mid-history by definition):
  // streaming output must not pull the view down — the jump-to-latest pill is
  // the way back, mirroring how a manual scroll-up behaves.
  const restoreAnchor = useCallback(
    (index: number, anchor: ScrollAnchor): void => {
      const el = scrollerRef.current
      if (!el) return
      // One settle loop at a time; a new restore supersedes any in flight.
      if (settleRafRef.current) cancelAnimationFrame(settleRafRef.current)
      settleRafRef.current = 0
      settleGateRef.current = true
      settleMeasuringRef.current = false
      // Drop the prepend/shift capture this restore SUPERSEDES. Those captures
      // say "keep the reader where they were before rows were inserted above
      // them"; a restore instead places them at an absolute offset priced
      // against the transcript that already contains those rows, so consuming
      // the capture afterwards adds the same block twice -- measured as
      // `WRITE restore 3021->965` answered by `WRITE reprice2 965->20211`.
      //
      // Cleared HERE rather than by making the compensations stand down for the
      // whole restore window: a prepend that arrives later has its own baseline
      // and must still be compensated, and blinding them for 600ms left those
      // uncompensated -- which walked the reader up a page per load and reopened
      // the older-history door, loading history on its own.
      dropShiftCapture()
      const count = itemsRef.current.length
      setWindowRange(computeJumpWindow(index, count, overscan))
      stickRef.current = false
      setIsAtBottom(false)
      // Initial position from offset math, synchronously (pre-paint — the
      // first painted frame is already at the restored position, no flash):
      // scrollTop such that the row's content offset sits `anchor.top` px
      // below the viewport top. The browser clamps an out-of-range value
      // against the not-yet-committed jump window; the settle frames re-land
      // it once the new spacers have committed.
      //
      // Accounted as 'pin': this is OUR positioning write, so the follow
      // guard must classify the resulting scroll event as self-scroll rather
      // than user input (stick is already false; recording the position does
      // not re-arm it — evaluateAutoPin never pins with stick released).
      const idxTree = heightIndexRef.current
      const off = idxTree ? idxTree.offsetOf(index) : getOffsetFn(index, count, getH)
      const target = Math.max(0, off - anchor.top)
      writeScrollTop(el, target, 'auto', 'pin', 'restore')
      // The write clamps against the CURRENT (pre-jump-window) geometry; align
      // the self-scroll reference with the value that actually landed so the
      // resulting scroll event is classified as ours, not user input (which
      // would trip the settle frames' user-scroll abort below).
      lastWriteTopRef.current = el.scrollTop
      lastWriteClientHRef.current = el.clientHeight
      // Settle: until it converges or ANCHOR_RESTORE_SETTLE_MS runs out, correct
      // against the anchor row's LIVE DOM position as measurements land (rows
      // above it refine from estimates). Aborts on hard user input (see the
      // loop's own check), a session change, a disconnected scroller, or the row
      // no longer answering to the anchor.
      // A degenerate rect (height 0 — jsdom, or not yet laid out) skips the
      // correction rather than applying garbage.
      const startedAt = typeof performance !== 'undefined' ? performance.now() : Date.now()
      const session = sessionIdRef.current
      let n = 0
      // Last frame's scroll extent. Convergence needs BOTH the row landing where
      // the anchor says AND the cause of the corrections having stopped -- see
      // anchorSettleConverged. The cause is height arriving ABOVE the anchor, not
      // the transcript growing: appends land below it and never move it, so
      // watching total height made convergence unreachable during a live turn.
      // Tracked as the anchor's own content offset, which our corrective writes
      // leave unchanged by construction.
      let lastAbove = Number.NaN
      const settle = () => {
        settleRafRef.current = 0
        const lower = () => {
          // The settle is no longer correcting, so the other compensations resume.
          settleMeasuringRef.current = false
          if (!settleGateRef.current) return
          settleGateRef.current = false
          returnRestoreRef.current = false
          setRestoreEval((v) => v + 1)
        }
        if (!el.isConnected) { if (n === 0) devLog('SETTLE.x', 'disconnected'); lower(); return }
        if (sessionIdRef.current !== session) { if (n === 0) devLog('SETTLE.x', 'session-changed'); lower(); return }
        // Aborts on a HARD input -- wheel / touchmove / scrollbar drag / scrolling
        // keys -- not on any scroll EVENT. Repositioning necessarily produces
        // scroll events of its own: committing the jump window swaps the spacers,
        // hydration grows the list under us (measured 6 rows to 17 during a single
        // restore), and native scroll anchoring adjusts scrollTop to keep visible
        // content stable. None of that is the reader, but all of it stamps
        // `lastUserScrollAtRef` -- so the loop was aborting on its own side
        // effects, measured 86ms in, leaving the reader wherever the estimate math
        // had put them. That is not a polish step being skipped: the corrections
        // this loop applies were measured at +111, -1035, -1792 and -2841px,
        // because the initial write is offset math over unmeasured rows and
        // routinely clamps to the bottom. Two entries with the same anchor and the
        // same row index landed 111px apart purely on whether this abort fired.
        if (lastHardInputAtRef.current > startedAt) {
          if (n === 0) devLog('SETTLE.x', `hardinput +${Math.round(lastHardInputAtRef.current - startedAt)}ms`)
          lower()
          return
        }
        // Resolved in the SAME vocabulary the anchor was persisted in -- the
        // stable id, not `getKey`. This check exists to abort when hydration
        // has moved another row into `index`; comparing a persisted stable id
        // against a per-render `getKey` can never match, so it aborted on the
        // FIRST frame every time and the settle correction below never ran at
        // all. That correction is the only thing that fixes the initial write:
        // the offset math runs against a height index still pricing unmeasured
        // rows at the estimate, so the reader lands near -- but not at -- where
        // they left, and the error grows with how much of the transcript above
        // them is still unmeasured.
        // BOTH identities, the same pair `findAnchorIndex` resolved with. Comparing
        // the tail alone disowns a row that was found through `alt` -- and it is
        // found that way precisely when the tail no longer matches, so this aborted
        // on frame 0 for every alt-resolved anchor and left the reader at the
        // estimate-based write this loop exists to correct.
        // The facade resolves both (anchoredRowIdentity), so the check has one
        // spelling beside the anchor's other identity lookups.
        const { rowId, matches } = anchoredRowIdentity(index, anchor)
        if (!matches) {
          if (n === 0) devLog('SETTLE.abort', `${keyShape(anchor.key)} vs ${rowId ? keyShape(rowId) : 'null'}`)
          lower(); return
        }
        let node: HTMLElement | null = null
        for (const [nEl, i] of elIndexRef.current.entries()) {
          if (i === index) { node = nEl as HTMLElement; break }
        }
        // Whether the OTHER compensations must stand down: only a settle that can
        // see its row is actually doing their job (see shiftCompensationAllowed).
        settleMeasuringRef.current = !!node
        if (!node && n === 0) {
          // Name the mounted range too: a missing node means the virtual window
          // does not contain the anchor row, and the window follows the scroll
          // position -- so this says whether the position went somewhere else.
          const mounted = Array.from(elIndexRef.current.values())
          const lo = mounted.length ? Math.min(...mounted) : -1
          const hi = mounted.length ? Math.max(...mounted) : -1
          devLog('SETTLE.x', `no-node idx=${index} mounted=${lo}..${hi} y=${Math.round(el.scrollTop)}`)
        }
        if (
          node &&
          typeof node.getBoundingClientRect === 'function' &&
          typeof el.getBoundingClientRect === 'function'
        ) {
          const rect = node.getBoundingClientRect()
          if (rect.height <= 0 && n === 0) devLog('SETTLE.x', 'rect0')
          if (rect.height > 0) {
            const delta = rect.top - el.getBoundingClientRect().top - anchor.top
            // The anchor's offset inside the content. `anchor.top` is constant, so
            // this moves only when the rows above it are repriced -- and not when
            // WE correct, because the write moves scrollTop by exactly `delta`.
            const aboveNow = delta + el.scrollTop
            const hasPrevious = Number.isFinite(lastAbove)
            const aboveDelta = hasPrevious ? aboveNow - lastAbove : 0
            lastAbove = aboveNow
            if (anchorSettleConverged({
              delta,
              aboveDelta,
              tolerance: ANCHOR_SETTLE_TOLERANCE_PX,
              hasPrevious,
            })) {
              if (settleGateRef.current && inspectorOn()) devLog('SETTLE.ok', `f${n} d=${delta.toFixed(1)} a=${Math.round(aboveNow)}`)
              // STOP, do not merely lower the gate. Convergence already requires
              // the height above the anchor to have stopped moving, so there is
              // nothing left to correct -- and once the gate is down the shift compensations
              // are permitted again, so a loop that keeps running becomes the
              // other half of a tug-of-war with them: captured on a phone as
              // `WRITE abovefold 6957->6933` answered by `WRITE settle
              // 6933->6957` for the rest of the budget, leaving the reader 24px
              // from where they had been.
              lower()
              return
            }
            if (Math.abs(delta) > ANCHOR_SETTLE_TOLERANCE_PX) {
              if (inspectorOn()) devLog('SETTLE', `f${n} ${delta > 0 ? '+' : ''}${Math.round(delta)}px`)
              writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin', 'settle')
            }
          }
        }
        n += 1
        const elapsed = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - startedAt
        if (elapsed < ANCHOR_RESTORE_SETTLE_MS) settleRafRef.current = requestAnimationFrame(settle)
        else { devLog('SETTLE.end', `${n} frames ${Math.round(elapsed)}ms`); lower() }
      }
      settleRafRef.current = requestAnimationFrame(settle)
    },
    [
      overscan, getH, scrollerRef, writeScrollTop, anchoredRowIdentity, dropShiftCapture,
      settleRafRef, settleGateRef, settleMeasuringRef, itemsRef, stickRef, heightIndexRef, lastWriteTopRef,
      lastWriteClientHRef, sessionIdRef, lastHardInputAtRef, elIndexRef, returnRestoreRef,
      setWindowRange, setIsAtBottom, setRestoreEval,
    ],
  )

  // ---- Slot entry: restore the saved reading position, else force the
  //      scroller to the true bottom ----
  // Runs after the new session's tail window has committed (windowRange reset
  // during render), before paint. Deterministic — does not inherit the
  // previous session's scrollTop (fixes the "second visit lands in the middle"
  // bug). Subsequent async widget growth is then followed by the RO via
  // pinAuto. A follow-up rAF settles after first-frame measurement.
  //
  // ALSO re-runs when items first arrive for a freshly-entered slot
  // (`sessionId` flips synchronously on slot switch, BEFORE the messages
  // HTTP fetch resolves — without the itemCount trigger forcePin would only
  // run against an empty list, leaving pinAuto to smooth-animate the
  // viewport down once content lands. That smooth scroll is the visible
  // "content scrolls from top to bottom" CX bug — and a late widget/image
  // measurement during the animation can land it short of the true bottom).
  // `slotPinDoneRef` guarantees the instant re-pin fires at most once per
  // slot entry; subsequent streaming appends still go through pinAuto.
  //
  // A latched reading-position anchor (pendingRestoreRef) takes precedence:
  // once items are present and the anchored row is found, restoreAnchor runs
  // INSTEAD of the bottom pin. While waiting for items, nothing pins — a
  // bottom pin's scroll events would let the debounced save clear the very
  // anchor being restored. An anchor whose row no longer exists (edited /
  // truncated transcript, or a non-durable minted key, or a race where the
  // key arrives with a later hydration chunk) falls back to the default pin.
  useLayoutEffect(() => {
    if (slotPinDoneRef.current && slotPinDoneRef.current !== sessionId) {
      slotPinDoneRef.current = null
    }
    if (scrollerRef.current) devWatchScroller(scrollerRef.current, itemCount)
    if (slotPinDoneRef.current === sessionId) return
    const anchor = pendingRestoreRef.current
    if (anchor && readerMovedSinceEntry()) {
      // The reader moved while the restore was owed: their position wins. Drop
      // the restore instead of landing it over them, and do not pin either --
      // the scroll handler already set `stick` from where they went. The save
      // (no longer gated by restoreOwnsPosition) clears the stored anchor at the
      // bottom or records the reader's row, so the next entry does not repeat it.
      devLog('RESTORE.drop', `${shortId(sessionId)} ${keyShape(anchor.key)} n=${itemCount}`)
      pendingRestoreRef.current = null
      returnRestoreRef.current = false
      if (restoreTimerRef.current !== null) {
        clearTimeout(restoreTimerRef.current)
        restoreTimerRef.current = null
      }
      slotPinDoneRef.current = sessionId
      scheduleAnchorSave()
      setRestoreEval((n) => n + 1)
      return
    }
    if (anchor) {
      const idx = itemCount > 0 ? findAnchorIndex(anchor) : -1
      if (idx >= 0) {
        devLog('RESTORE.OK', `${shortId(sessionId)} ${keyShape(anchor.key)} idx=${idx} n=${itemCount}`)
        pendingRestoreRef.current = null
        // `returnRestoreRef` is left as-is here: a return restore keeps its
        // settle loop (rows may re-measure after the rebuild) and the flag has
        // to outlive the pending latch so the settle window stays ungated too.
        // It drops when the settle loop ends.
        if (restoreTimerRef.current !== null) {
          clearTimeout(restoreTimerRef.current)
          restoreTimerRef.current = null
        }
        slotPinDoneRef.current = sessionId
        // The reader did not choose the position we are about to write -- WE did.
        // Revoke the gesture that brought them here (the tap that switched slots
        // stamps hard input, and the debounced save only asks whether SOME hard
        // input happened within its window) so our own placement cannot be
        // persisted as if it were a reading position. Without this the save fires
        // ~200ms later against a height index that is still resolving estimates
        // into measured rows, so it records a DRIFTED offset and the next return
        // starts from that: measured on device -746 -> -611 -> -453 across three
        // returns, the reader climbing ~150px further from the end each time.
        // The next save now waits for a real gesture, which is the only thing
        // that can express a position the reader actually picked.
        lastHardInputAtRef.current = Number.NEGATIVE_INFINITY
        restoreAnchor(idx, anchor)
        // Resolving the restore also lowers `restoreGate`, which is derived from
        // this ref -- bump so the caller re-renders and lifts its skeleton in the
        // commit that FOLLOWS the positioning write, never before it.
        setRestoreEval((n) => n + 1)
        return
      }
      // The anchored row is not on hand YET. A transcript hydrates in chunks, so
      // hold the anchor (and the caller's skeleton) rather than treating this
      // commit as the final word -- see RESTORE_HYDRATE_WAIT_MS. Nothing is
      // pinned while holding: `stick` was released at entry, so the reader is
      // not dragged to the bottom and then away from it again.
      // Growth RENEWS the wait. A fixed budget from entry was the wrong shape: it
      // has to cover however long the transcript takes to arrive, and that scales
      // with how much is loaded -- a slot holding thousands of messages hydrates
      // far past 1.2s, so the restore gave up and dropped the reader at the live
      // end, while a slot holding a few hundred worked. Asking whether rows are
      // still ARRIVING bounds it by the cause instead of by a guess: a row that is
      // genuinely gone still expires one budget after the last arrival.
      if (itemCount > restoreLastCountRef.current) {
        restoreLastCountRef.current = itemCount
        restoreDeadlineRef.current = performance.now() + RESTORE_HYDRATE_WAIT_MS
      }
      if (performance.now() < restoreDeadlineRef.current) {
        devLog('RESTORE.hold', `${shortId(sessionId)} ${keyShape(anchor.key)} n=${itemCount}`)
        // A hydration commit re-runs this effect by itself; the timer only has to
        // cover the case where growth STOPS before the row ever appears.
        if (restoreTimerRef.current === null) {
          const waitMs = Math.max(0, restoreDeadlineRef.current - performance.now())
          restoreTimerRef.current = setTimeout(() => {
            restoreTimerRef.current = null
            setRestoreEval((n) => n + 1)
          }, waitMs + 16)
        }
        return
      }
      // Deadline passed: the row is genuinely not coming (edited or truncated
      // transcript, a non-durable id, a session whose tail was replaced). Give
      // up deliberately -- re-arm follow, which the entry released in
      // anticipation, and take the default bottom placement.
      devLog('RESTORE.giveup', `${shortId(sessionId)} ${keyShape(anchor.key)} n=${itemCount}`)
      const _idFn = getStableIdRef.current
      const _its = itemsRef.current
      devLog('GIVEUP.rows', `${_its.length}: ${_its.slice(0, 7).map((it, i) => keyShape(_idFn ? _idFn(it, i) : getKeyRef.current(it, i))).join(' ')}`)
      pendingRestoreRef.current = null
      returnRestoreRef.current = false
      stickRef.current = followOutput
      setRestoreEval((n) => n + 1)
    }
    if (initialPlacement === 'top') {
      // Head placement: a fresh scroller already sits at 0, but an INHERITED
      // one (externalScrollerRef pointing at a page column that outlives this
      // hook) can carry leftover scrollTop from whatever it showed before.
      // Write 0 explicitly — accounted as 'pin' so the follow guard reads the
      // resulting scroll event as ours. No second-frame write is needed: at
      // the head there is nothing above the viewport to re-clamp against.
      if (itemCount === 0) return // wait for content; effect re-runs when items arrive
      slotPinDoneRef.current = sessionId
      const el = scrollerRef.current
      if (el && el.scrollTop !== 0) writeScrollTop(el, 0, 'auto', 'pin', 'top')
      return
    }
    forcePin()
    if (itemCount === 0) return  // wait for content; effect re-runs when items arrive
    slotPinDoneRef.current = sessionId
    const id = requestAnimationFrame(() => {
      const el = scrollerRef.current
      if (el && el.isConnected) forcePin()
    })
    return () => cancelAnimationFrame(id)
    // `restoreEval` is what the expiry timer and each resolution re-enter through.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, scrollerEl, itemCount, restoreEval])
}
