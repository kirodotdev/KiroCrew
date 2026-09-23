import { useLayoutEffect, useRef, type RefObject } from 'react'

/**
 * Hold the reader's place across the artifact/activity SIDE PANEL opening or
 * closing.
 *
 * The side panel is a layout sibling of the transcript column, so toggling it
 * changes the transcript scroller's `clientWidth`. That width feeds the height
 * cache's scope key (`scrollerWidthBucket` in ChatPage); crossing a bucket
 * boundary rebuilds the virtualizer's height index from coarse estimates. Every
 * OTHER height-mutating path in the virtualizer wraps its commit in an anchor
 * capture + `scrollTop` compensation — the width-bucket swap did not. For a
 * reader parked at the bottom the follow pin hides it, but a reader scrolled up
 * into history has follow released, so nothing re-pins them and their fixed
 * `scrollTop` lands on re-estimated content: the transcript "jumps" and strands
 * them (measured ~780px, scrollTop → 0, closing a panel on a long session).
 *
 * The left nav rail solves the analogous reflow with `useRailWidth`'s settle
 * window; the settle window only suppresses the per-frame thrash and only
 * re-pins a FOLLOWING reader, so it does not cover the scrolled-up case this
 * fixes. This hook is deliberately scoped to the side panel — the more obvious,
 * reliably reproducible cause — and preserves position for the released reader
 * specifically.
 *
 * Mechanism: when `wantsMount` toggles, capture the top visible row's key and
 * its offset from the scroller top BEFORE the panel's width animation paints
 * (the layout effect runs before the browser applies the animated width, and
 * the bucket flip is debounced ~200ms behind it, so the capture reads
 * pre-reprice geometry). Then poll by animation frame, tracking WHEN the width
 * last moved. ChatPage rebuilds the height index on a 200ms-debounced
 * ResizeObserver behind that last width change, so the correction waits until
 * that debounce plus a re-render slack has elapsed since the last width change
 * — NOT merely until the width stops moving, which happens ~20ms after the
 * animation and well before the reprice — then, once the row's offset has also
 * stopped moving, corrects `scrollTop` by the residual so that same row returns
 * to its captured offset. A hard time ceiling bounds the wait. A real scroll
 * gesture during the window, a session switch, or the row leaving the mounted
 * set abandons the correction. Skipped while following, where the bottom pin
 * already owns position.
 */

interface RowAnchor {
  /** The virtual row's display index — stable across a width reprice because
   *  the item list is unchanged, only row heights are. */
  index: string
  /** The anchor row's top, in px, relative to the scroller's client top. */
  offset: number
}

/** Find the first row intersecting the viewport top and return its display
 *  index and offset. Rows carry `data-display-index` in the transcript render,
 *  so the same row is re-findable after the reprice (the panel toggle changes
 *  heights, not the item list). */
function captureTopRow(scroller: HTMLElement): RowAnchor | null {
  const scRect = scroller.getBoundingClientRect()
  const rows = scroller.querySelectorAll<HTMLElement>('[data-display-index]')
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i]
    const rr = row.getBoundingClientRect()
    // First row whose bottom is still on screen and whose top has not passed
    // the bottom edge — i.e. the topmost visible row.
    if (rr.bottom > scRect.top + 2 && rr.top < scRect.bottom) {
      const index = row.getAttribute('data-display-index')
      if (index) return { index, offset: rr.top - scRect.top }
    }
  }
  return null
}

/** Current offset of the row with `index`, or null if it is not mounted. */
function offsetOf(scroller: HTMLElement, index: string): number | null {
  const row = scroller.querySelector<HTMLElement>(`[data-display-index="${CSS.escape(index)}"]`)
  if (!row) return null
  return row.getBoundingClientRect().top - scroller.getBoundingClientRect().top
}

export interface SidePanelScrollAnchorOptions {
  /** True while the side panel is (or wants to be) mounted. Its transition is
   *  the reflow trigger. */
  wantsMount: boolean
  /** Live transcript scroller element. */
  scrollerRef: RefObject<HTMLElement | null>
  /** Returns true when the reader is following the live end (pinned to bottom).
   *  A follower's position is owned by the bottom pin, so we do not compensate
   *  — matching the rail settle window, which only re-pins a follower. */
  isFollowing: () => boolean
  /** Active session/slot key. If it changes during the settle window the
   *  transcript is now a different conversation, so the captured anchor is
   *  meaningless and the pending correction is abandoned. */
  sessionKey: string | null | undefined
}

/** ChatPage debounces the `scrollerWidthBucket` update — the thing that rebuilds
 *  the virtualizer's `HeightIndex` — 200ms behind the LAST width change (its
 *  ResizeObserver timer). The panel width itself animates up to ~400ms. The
 *  correction must not fire until the debounced reprice has had time to land, so
 *  the settle gate is time-based, keyed off the last observed width change:
 *  waiting only for width-frame stability exits ~20ms after the animation stops,
 *  well before the reprice, and re-jumps the reader. */
const REPRICE_DEBOUNCE_MS = 200
/** Slack past the debounce for the state update + re-render + re-measure to
 *  commit before we read the "final" offset. */
const REPRICE_COMMIT_SLACK_MS = 80
/** Hard ceiling on the whole settle wait: longest animation (~400ms) + debounce
 *  (200ms) + slack, with margin. Bails cleanly if the reprice never comes (e.g.
 *  the width did not cross a bucket boundary) or the row was evicted. */
const MAX_SETTLE_MS = 900

export function useSidePanelScrollAnchor({
  wantsMount,
  scrollerRef,
  isFollowing,
  sessionKey,
}: SidePanelScrollAnchorOptions): void {
  const rafRef = useRef<number | null>(null)
  // Kept live every render so the settle loop (keyed only on wantsMount) sees a
  // session switch that happens WHILE it is running.
  const sessionKeyRef = useRef(sessionKey)
  sessionKeyRef.current = sessionKey

  useLayoutEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    if (isFollowing()) return
    if (typeof requestAnimationFrame === 'undefined') return

    const anchor = captureTopRow(scroller)
    if (!anchor) return

    const sessionAtCapture = sessionKeyRef.current
    const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
    const startedAt = now()
    const startWidth = scroller.clientWidth
    let widthChanged = false
    let lastWidth = startWidth
    // When the width last MOVED. The reprice is debounced behind this, so the
    // correction waits REPRICE_DEBOUNCE_MS + slack past it, not past frame count.
    let lastWidthChangeAt = startedAt
    let stableOffsetFrames = 0
    let lastOffset: number | null = null
    let aborted = false

    // A deliberate scroll gesture during the settle window means the reader
    // chose a new position; treating it as reflow would yank them back to the
    // captured anchor. Any of these cancels the pending correction. (Being a
    // non-follower is a STATE, not a gesture — a reader already scrolled up
    // stays non-following, so isFollowing() alone cannot detect intent here.)
    const onIntent = () => { aborted = true }
    const intentEvents: Array<keyof HTMLElementEventMap> = [
      'wheel', 'touchstart', 'pointerdown', 'keydown',
    ]
    for (const ev of intentEvents) {
      scroller.addEventListener(ev, onIntent, { passive: true })
    }
    const removeIntent = () => {
      for (const ev of intentEvents) scroller.removeEventListener(ev, onIntent)
    }

    const step = () => {
      rafRef.current = null
      const el = scrollerRef.current
      // A gesture, a follow re-pin, a session switch, or a lost scroller all
      // mean the captured anchor no longer describes what the reader wants.
      if (aborted || !el || isFollowing() || sessionKeyRef.current !== sessionAtCapture) {
        removeIntent()
        return
      }

      const t = now()
      const width = el.clientWidth
      if (width !== lastWidth) {
        widthChanged = true
        lastWidthChangeAt = t
        lastWidth = width
      }

      if (t - startedAt >= MAX_SETTLE_MS) {
        // Ceiling reached: apply whatever residual we can (a no-op if the width
        // never crossed a bucket boundary and nothing repriced) and stop.
        const cur = offsetOf(el, anchor.index)
        if (widthChanged && cur != null) {
          const delta = cur - anchor.offset
          if (Math.abs(delta) >= 1) el.scrollTop += delta
        }
        removeIntent()
        return
      }

      const current = offsetOf(el, anchor.index)
      if (current == null) {
        // Row not mounted yet (or evicted). Keep waiting until the ceiling.
        rafRef.current = requestAnimationFrame(step)
        return
      }

      // The reprice is debounced REPRICE_DEBOUNCE_MS behind the LAST width
      // change and then needs a re-render + re-measure to commit. Only measure
      // offset stability once that window has elapsed — this is what stops the
      // loop from exiting on a steady pre-reprice offset ~20ms after the
      // animation stops, before the bucket flip rebuilds the height index.
      const repriceSettled =
        widthChanged && t - lastWidthChangeAt >= REPRICE_DEBOUNCE_MS + REPRICE_COMMIT_SLACK_MS
      if (repriceSettled) {
        if (lastOffset != null && Math.abs(current - lastOffset) < 1) {
          stableOffsetFrames += 1
        } else {
          stableOffsetFrames = 0
        }
        lastOffset = current

        if (stableOffsetFrames >= 1) {
          const delta = current - anchor.offset
          if (Math.abs(delta) >= 1) el.scrollTop += delta
          removeIntent()
          return
        }
      } else {
        // Still inside the animation/debounce window — track the latest offset
        // so post-settle stability is measured only against post-reprice frames.
        lastOffset = current
        stableOffsetFrames = 0
      }

      rafRef.current = requestAnimationFrame(step)
    }

    rafRef.current = requestAnimationFrame(step)
    return () => {
      removeIntent()
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
    }
    // Intentionally keyed ONLY on wantsMount: this must run once per panel
    // open/close transition, not on unrelated scroller/follow/session churn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantsMount])
}
