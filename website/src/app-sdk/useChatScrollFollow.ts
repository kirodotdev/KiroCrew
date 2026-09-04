/**
 * useChatScrollFollow — stick-to-bottom follow for a PLAIN (non-virtualized)
 * chat scroller.
 *
 * The render-layer counterpart to `useVirtualChat` for hosts that render the
 * full message list in one overflow-y-auto div (ChatPane, ChatEmbed). It reuses
 * FollowController's pure decision core — the same race-proof "stick" model the
 * main chat's virtualizer runs on — so every chat surface follows and releases
 * with identical semantics:
 *
 *   - Follow is released ONLY by a genuine user scroll away from the bottom,
 *     and re-engaged by returning to the bottom (or an explicit jump).
 *   - Content changes (streamed chunks, tool-result growth on EARLIER rows,
 *     turn-collapse SHRINK when a turn completes) are observed via a
 *     ResizeObserver on the content wrapper, not by hashing the tail message —
 *     a mid-list mutation that leaves the last message untouched still re-pins.
 *   - A content shrink while following re-pins to the new bottom instead of
 *     stranding the viewport (the "transcript suddenly got shorter" jump).
 *
 * INVARIANT (inherited from useVirtualChat): every programmatic scrollTop
 * write records itself in `lastWriteTopRef`, and all pins are INSTANT — the
 * self-scroll guard is only reliable because there is never an in-flight
 * animation to desynchronise the reference.
 *
 * Render contract:
 *   - Attach `scrollerRef` + `onScroll` to the overflow-y-auto container.
 *   - Attach `contentRef` to a wrapper around the scrollable content.
 *   - `isAtBottom` drives the jump-to-bottom pill; `scrollToBottom()` is the
 *     pill's action (re-arms follow).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  DEFAULT_BOTTOM_THRESHOLD,
  bottomTarget,
  computeAtBottom,
  evaluateAutoPin,
  isSelfScroll,
  resolveUserScrollStick,
  type ScrollGeom,
} from '../hooks/virtualizer/FollowController'

export interface ChatScrollFollowApi {
  /** Attach to the overflow-y-auto scroll container. */
  scrollerRef: React.MutableRefObject<HTMLDivElement | null>
  /** Attach to the inner wrapper around the scrollable content (RO target). */
  contentRef: (node: HTMLDivElement | null) => void
  /** Wire to the scroll container's onScroll. */
  onScroll: () => void
  /** Within DEFAULT_BOTTOM_THRESHOLD of the bottom (drives the jump pill). */
  isAtBottom: boolean
  /** Explicit jump to the bottom; always lands there and re-arms follow. */
  scrollToBottom: () => void
}

function readGeom(el: HTMLElement): ScrollGeom {
  return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
}

export function useChatScrollFollow(opts: {
  /** Identity of the conversation shown; changing it force-pins to the bottom. */
  resetKey?: string
  /** Off = the hook is fully inert: no mount pin, no ResizeObserver, no scroll
   *  handling, `isAtBottom` frozen true. An explicit switch rather than "which
   *  refs you attach", so a host with a mode (ChatEmbed's startAtBottom) cannot
   *  half-wire it — an attached scroller with a disabled mode must never pin. */
  enabled?: boolean
} = {}): ChatScrollFollowApi {
  const { resetKey, enabled = true } = opts
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const [contentEl, setContentEl] = useState<HTMLDivElement | null>(null)
  const stickRef = useRef(true)
  const lastWriteTopRef = useRef(-1)
  // The scroller's `clientHeight` at the moment `lastWriteTopRef` was recorded
  // — the viewport box that value was a bottom FOR. Kept in lockstep with it
  // (`-1` alongside `-1`) so the pin evaluation can tell how much of the
  // current distance-from-bottom is our own viewport shrink rather than the
  // user's move (see evaluateAutoPin's `viewportShrink`). This observer watches
  // the scroller's own box as well as the content wrapper, so a pane drag or a
  // keyboard opening below the transcript shrinks it with no user input.
  const lastWriteClientHRef = useRef(-1)
  const prevScrollTopRef = useRef(-1)
  const [isAtBottom, setIsAtBottom] = useState(true)
  // Effect-stable mirror so the callbacks read the live value without
  // re-creating per flip.
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  const writePin = useCallback((el: HTMLElement, target: number) => {
    el.scrollTop = target
    lastWriteTopRef.current = target
    lastWriteClientHRef.current = el.clientHeight
    prevScrollTopRef.current = target
  }, [])

  /** Automatic pin at a content-change moment (RO tick). Pure decision in
   *  FollowController.evaluateAutoPin; this only reads live geometry and acts. */
  const pinAuto = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    const geom = readGeom(el)
    const result = evaluateAutoPin({
      stick: stickRef.current,
      geom,
      lastWriteTop: lastWriteTopRef.current,
      // Measure the scroll-up guard against the box our reference was a bottom
      // for, never the box that just shrank. A turn-collapse SHRINK clamps
      // scrollTop below our last write while leaving us at the new bottom; if
      // the scroller's own box shrinks in the SAME tick, the clamp plus the
      // shrink-inflated distance together look exactly like a user scrolling
      // up, and follow releases for good with nothing to re-arm it.
      viewportShrink:
        lastWriteClientHRef.current >= 0 ? lastWriteClientHRef.current - geom.clientHeight : 0,
    })
    stickRef.current = result.stick
    if (result.pin) {
      writePin(el, result.target)
    } else if (result.stick) {
      // Following and already at the bottom — keep the self-scroll reference
      // aligned so the next scroll event is not misread as the user's.
      lastWriteTopRef.current = result.target
      lastWriteClientHRef.current = geom.clientHeight
    }
    // Content growth while the user is scrolled up must reveal the jump pill
    // even though no scroll event fires.
    setIsAtBottom(computeAtBottom(readGeom(el), DEFAULT_BOTTOM_THRESHOLD))
  }, [writePin])

  // Scroller height as of the last SCROLL event. Deliberately separate from
  // `lastWriteClientHRef` (which tracks our own writes) and from anything the
  // ResizeObserver touches: a viewport resize and the clamp it causes are two
  // events, so a baseline the observer could advance first would fold the growth
  // away before the clamp's scroll event asked about it.
  const lastScrollClientHRef = useRef(0)

  const onScroll = useCallback(() => {
    if (!enabledRef.current) return
    const el = scrollerRef.current
    if (!el) return
    const geom = readGeom(el)
    setIsAtBottom(computeAtBottom(geom, DEFAULT_BOTTOM_THRESHOLD))
    // Our own pins fire scroll events too; only a USER scroll may flip stick.
    if (!isSelfScroll(geom.scrollTop, lastWriteTopRef.current)) {
      stickRef.current = resolveUserScrollStick({
        stick: stickRef.current,
        followOutput: true,
        scrollTop: geom.scrollTop,
        prevScrollTop: prevScrollTopRef.current,
        geom,
        // A taller viewport lowers the maximum scrollTop, so the engine clamps a
        // near-bottom reader flush with no write to observe. This hook observes
        // exactly the changes that cause it — pane resizes and the soft keyboard —
        // so without the signal a clamp reads as the reader returning to the
        // bottom and re-arms follow for someone who never touched the scroller.
        viewportGrowth:
          lastScrollClientHRef.current > 0
            ? geom.clientHeight - lastScrollClientHRef.current
            : 0,
      })
      // A user scroll invalidates the self-scroll reference: keeping it would
      // let a later user move back to the same offset read as ours.
      if (!stickRef.current) {
        lastWriteTopRef.current = -1
        lastWriteClientHRef.current = -1
      }
    }
    prevScrollTopRef.current = geom.scrollTop
    lastScrollClientHRef.current = geom.clientHeight
  }, [])

  const scrollToBottom = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    stickRef.current = true
    writePin(el, bottomTarget(readGeom(el)))
    setIsAtBottom(true)
  }, [writePin])

  // Conversation switch: land at the bottom with follow re-armed, exactly like
  // slot entry in the main chat. Refs reset first so stale guards from the
  // previous conversation cannot suppress the pin.
  useEffect(() => {
    if (!enabled) return
    stickRef.current = true
    lastWriteTopRef.current = -1
    lastWriteClientHRef.current = -1
    prevScrollTopRef.current = -1
    setIsAtBottom(true)
    const el = scrollerRef.current
    if (el) writePin(el, bottomTarget(readGeom(el)))
  }, [resetKey, enabled, writePin])

  // One observer over both the content wrapper (height changes: streaming,
  // hydrate, turn collapse, image/widget load) and the scroller itself
  // (viewport resizes: pane drag, keyboard). Every size change re-evaluates
  // the pin; observe() fires an initial tick, which is what lands the first
  // hydrate at the bottom.
  useEffect(() => {
    if (!enabled) return
    const scroller = scrollerRef.current
    if (typeof ResizeObserver === 'undefined' || (!contentEl && !scroller)) return
    const ro = new ResizeObserver(() => pinAuto())
    if (contentEl) ro.observe(contentEl)
    if (scroller) ro.observe(scroller)
    return () => ro.disconnect()
  }, [contentEl, enabled, pinAuto])

  return { scrollerRef, contentRef: setContentEl, onScroll, isAtBottom, scrollToBottom }
}
