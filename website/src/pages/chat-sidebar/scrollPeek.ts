import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { loadChatConfig } from '../chat/ChatSettings'

/**
 * Scroll peek for the sessions sidebar.
 *
 * Desktop list lanes only. The peek exists to draw a clipped title past the
 * sidebar's edge, over the chat beside it. A phone's sessions pane is a
 * near-full-width drawer with no room beside it, so the caller leaves the
 * peek off there; the row's `title` attribute and the rename box remain the
 * way to the full string.
 *
 * Session titles never wrap (one line, `truncate`), so a narrow sidebar clips
 * most of them. While the user scrolls the session lane, each title in view
 * that the row clips is shown in full in a floating layer laid exactly over
 * it (see ScrollPeekLayer). The rows themselves never change: the card keeps
 * the user's stored width, nothing scrolls horizontally, and the layer is
 * click-through, so a click lands on whatever is under it (the row, or the
 * chat beside the sidebar).
 *
 * Lifecycle:
 * - wheel / touchmove / a scrolling key inside the lane -> the peek turns on,
 *   and the titles in view are measured on the next frame (after the input's
 *   own scroll);
 * - while it is on, every lane scroll and every change to the lane's rows
 *   (a live re-sort, a mount, a rename) re-measures, so the layer follows the
 *   virtualized rows as they mount, unmount and move;
 * - the row under the pointer shows no chip, so its hover actions stay visible;
 * - a title behind a stuck folder header shows no chip;
 * - pointer over the sidebar -> stays on, up to PEEK_IDLE_MAX_MS after the
 *   last scroll input;
 * - any press on the page, any key that does not scroll the lane, or the
 *   window losing focus -> off at once (the layer paints above the shell, so
 *   nothing may open beneath it);
 * - pointer leaves -> off after PEEK_LEAVE_DELAY_MS; input with no pointer over
 *   the card (touch, keyboard) -> off PEEK_COLLAPSE_DELAY_MS after the last
 *   input. Focus alone never holds it on.
 */

/** A title clipped by less than this many px is left alone. */
export const PEEK_MIN_OVERFLOW = 16
/** Grace period after the last scroll input with no pointer over the card
 *  (touch, keyboard): long enough to read what the peek showed. */
export const PEEK_COLLAPSE_DELAY_MS = 700
/** Off delay once the pointer leaves the card. */
export const PEEK_LEAVE_DELAY_MS = 150
/** Ceiling on a peek held by a resting pointer: this long after the last
 *  scroll input it ends regardless, so the layer (which paints above the
 *  shell) cannot cover a banner or pill that mounts on its own indefinitely. */
export const PEEK_IDLE_MAX_MS = 4000
/** Gap kept between a peeked title and the window's right edge. */
const WINDOW_MARGIN_PX = 12

/** One clipped title, in viewport coordinates, ready for the layer to draw. */
export interface PeekTitle {
  /** `data-session-row` identity of the row (plus its scope), for React keys. */
  key: string
  text: string
  left: number
  top: number
  height: number
  /** Full single-line width the title wants. */
  width: number
  /** Most the layer may draw before the window edge. */
  maxWidth: number
}

/**
 * Every title in view in `lane` that its row clips by PEEK_MIN_OVERFLOW px or more.
 *
 * `hoveredRow` (the row under the pointer) is left out: its hover actions
 * (menu, copy, close) sit at the row's right edge, and a chip over them would
 * hide controls that still take the click.
 */
export function measureClippedTitles(lane: HTMLElement, windowWidth: number, hoveredRow: Element | null = null): PeekTitle[] {
  const laneRect = lane.getBoundingClientRect()
  // The search dock floats over the lane's top; the lane reserves it as
  // scroll padding, so a row under the dock is not in view.
  const dockTop = laneRect.top + (parseFloat(getComputedStyle(lane).scrollPaddingTop) || 0)
  // A stuck folder header pins over the rows scrolling beneath it. Rows never
  // overlap one another in flow, so a title intersecting a header is hidden
  // behind it.
  const headers = Array.from(lane.querySelectorAll<HTMLElement>('.folder-row-sticky'), h => h.getBoundingClientRect())
  const out: PeekTitle[] = []
  for (const el of lane.querySelectorAll<HTMLElement>('[data-session-title]')) {
    if (el.scrollWidth - el.clientWidth < PEEK_MIN_OVERFLOW) continue
    const r = el.getBoundingClientRect()
    // Only rows fully inside the lane's visible band: a half-scrolled row would
    // float its title over the sidebar header or the search dock.
    if (r.top < dockTop || r.bottom > laneRect.bottom) continue
    if (headers.some(h => h.height > 0 && r.top < h.bottom && r.bottom > h.top)) continue
    // The layer paints above the whole shell. A title whose start something
    // else covers (a docked panel over the sidebar's last rows) stays covered.
    // Only the title's start is sampled: a panel over just the part the chip
    // draws past the sidebar's edge is not detected, and is bounded by
    // PEEK_IDLE_MAX_MS instead.
    const hit = document.elementFromPoint?.(r.left + Math.min(r.width, 24) / 2, r.top + r.height / 2)
    if (hit && hit !== document.body && hit !== document.documentElement && !lane.contains(hit)) continue
    const row = el.closest<HTMLElement>('[data-session-row]')
    if (hoveredRow && row === hoveredRow) continue
    out.push({
      key: `${row?.dataset.sessionScope ?? ''}:${row?.dataset.sessionRow ?? out.length}`,
      text: el.textContent ?? '',
      left: r.left,
      top: r.top,
      height: r.height,
      width: el.scrollWidth,
      maxWidth: Math.max(0, windowWidth - r.left - WINDOW_MARGIN_PX),
    })
  }
  return out.length === 0 ? NONE : out
}

/** The session row under the viewport point, if any. Resolved from row
 *  rects rather than hover state: a browser does not update `:hover` (or fire
 *  `pointerover`) while the wheel scrolls rows under a still pointer. */
export function rowAtPoint(lane: HTMLElement, point: { x: number; y: number } | null): Element | null {
  if (!point) return null
  for (const row of lane.querySelectorAll('[data-session-row]')) {
    const r = row.getBoundingClientRect()
    if (point.x >= r.left && point.x < r.right && point.y >= r.top && point.y < r.bottom) return row
  }
  return null
}

/** Keys that scroll the lane (row navigation included), so a keyboard user
 *  gets the same peek a wheel does. */
function isScrollKey(key: string): boolean {
  return key === 'ArrowUp' || key === 'ArrowDown' || key === 'PageUp' || key === 'PageDown' || key === 'Home' || key === 'End'
}

/** A caret key typed into a text field (the row rename box) moves the caret,
 *  not the lane. */
function isTextEntry(target: EventTarget | null): boolean {
  return target instanceof HTMLElement
    && (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target.isContentEditable)
}

interface Options {
  /** Off: no listeners, and a live peek ends at once. */
  enabled: boolean
  /** The sidebar root: pointer presence and presses are tracked here, and
   *  scroll input is listened for here (it bubbles from the lane). */
  rootRef: RefObject<HTMLElement | null>
  /** The scrolling session lane; input outside it (header, dock) is ignored. */
  laneRef: RefObject<HTMLElement | null>
}

const NONE: PeekTitle[] = []

function samePeek(a: readonly PeekTitle[], b: readonly PeekTitle[]): boolean {
  return a.length === b.length && a.every((t, i) => {
    const u = b[i]
    return t.key === u.key && t.text === u.text && t.left === u.left && t.top === u.top
      && t.height === u.height && t.width === u.width && t.maxWidth === u.maxWidth
  })
}

/** The clipped titles to show in full right now; empty while the peek is off. */
export function useSidebarScrollPeek({ enabled, rootRef, laneRef }: Options): PeekTitle[] {
  const [titles, setTitles] = useState<PeekTitle[]>(NONE)
  const activeRef = useRef(false)
  const frameRef = useRef<number | null>(null)
  const pointerInsideRef = useRef(false)
  /** Last mouse/pen position over the sidebar; null when it is elsewhere. */
  const pointRef = useRef<{ x: number; y: number } | null>(null)
  const rowObserverRef = useRef<MutationObserver | null>(null)
  const collapseTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const clearCollapse = () => {
    if (collapseTimer.current !== null) { clearTimeout(collapseTimer.current); collapseTimer.current = null }
  }

  const clearIdle = () => {
    if (idleTimer.current !== null) { clearTimeout(idleTimer.current); idleTimer.current = null }
  }

  const stop = useCallback(() => {
    clearCollapse()
    clearIdle()
    activeRef.current = false
    rowObserverRef.current?.disconnect()
    if (frameRef.current !== null) { cancelAnimationFrame(frameRef.current); frameRef.current = null }
    setTitles(NONE)
  }, [])

  const scheduleStop = useCallback((delay: number) => {
    clearCollapse()
    collapseTimer.current = setTimeout(() => {
      collapseTimer.current = null
      if (!pointerInsideRef.current) stop()
    }, delay)
  }, [stop])

  /** (Re)arm the idle ceiling; called on every scroll input. */
  const touchIdle = useCallback(() => {
    clearIdle()
    idleTimer.current = setTimeout(() => { idleTimer.current = null; stop() }, PEEK_IDLE_MAX_MS)
  }, [stop])

  useEffect(() => {
    const root = rootRef.current
    if (!enabled || !root) {
      stop()
      return
    }
    // One measure per frame, after the frame's scroll has been applied.
    const remeasure = () => {
      if (frameRef.current !== null) return
      frameRef.current = requestAnimationFrame(() => {
        frameRef.current = null
        const lane = laneRef.current
        if (!activeRef.current || !lane) return
        const next = measureClippedTitles(lane, window.innerWidth, rowAtPoint(lane, pointRef.current))
        // Same chips -> keep the old array, so a re-render's own style writes
        // (watched below) cannot drive a measure loop.
        setTitles(prev => (samePeek(prev, next) ? prev : next))
      })
    }
    // Rows can move with no scroll at all (a live recency re-sort, a row
    // mounting or renaming): follow the lane's DOM while the peek is on.
    // `style` is watched too: a re-sort slides rows with a per-frame transform,
    // and following it keeps each chip on its row until the slide settles.
    const rowObserver = new MutationObserver(() => { if (activeRef.current) remeasure() })
    rowObserverRef.current = rowObserver
    const observeLane = () => {
      const lane = laneRef.current
      if (lane) rowObserver.observe(lane, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['style'] })
    }
    const onScrollIntent = (e: Event) => {
      const lane = laneRef.current
      if (!lane || !(e.target instanceof Node) || !lane.contains(e.target)) return
      // ctrl/cmd+wheel is page zoom, not a scroll of the list.
      if (e instanceof WheelEvent && (e.deltaY === 0 || e.ctrlKey || e.metaKey)) return
      if (e instanceof KeyboardEvent && (!isScrollKey(e.key) || e.altKey || e.ctrlKey || e.metaKey || isTextEntry(e.target))) return
      if (lane.scrollHeight <= lane.clientHeight) return
      if (e instanceof WheelEvent) pointRef.current = { x: e.clientX, y: e.clientY }
      if (!activeRef.current) observeLane()
      activeRef.current = true
      touchIdle()
      if (!pointerInsideRef.current) scheduleStop(PEEK_COLLAPSE_DELAY_MS)
      remeasure()
    }
    // The lane's own scroll event (any cause, momentum included) moves the
    // rows: follow them while the peek is on.
    const onLaneScroll = (e: Event) => {
      if (activeRef.current && e.target === laneRef.current) remeasure()
    }
    // A press anywhere on the page ends the reading: choosing a row, the
    // resize grip, a folder toggle, or opening something elsewhere. The layer
    // paints above the whole shell, so it must be gone before an overlay
    // (notifications, a dialog) opens beneath it.
    const onPress = () => { if (activeRef.current) stop() }
    // Likewise any key that is not a scroll of the lane (a shortcut that opens
    // a sheet or palette, typing elsewhere).
    const onAnyKey = (e: KeyboardEvent) => {
      if (!activeRef.current) return
      const lane = laneRef.current
      const scrollsLane = lane && e.target instanceof Node && lane.contains(e.target)
        && isScrollKey(e.key) && !e.altKey && !e.ctrlKey && !e.metaKey && !isTextEntry(e.target)
      if (!scrollsLane) stop()
    }
    // Hover is a mouse/pen notion: a touch contact fires enter/leave around
    // every tap, and would cut the touch reading time to the leave delay.
    const onEnter = (e: PointerEvent) => { if (e.pointerType === 'touch') return; pointerInsideRef.current = true; clearCollapse() }
    const onLeave = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
      pointerInsideRef.current = false
      pointRef.current = null
      if (activeRef.current) scheduleStop(PEEK_LEAVE_DELAY_MS)
    }
    // The row under the pointer drops its chip, so its hover actions show.
    // Every measure re-resolves that row from the pointer position.
    const onMove = (e: PointerEvent) => {
      if (e.pointerType === 'touch') return
      pointRef.current = { x: e.clientX, y: e.clientY }
      if (activeRef.current) remeasure()
    }
    const onWindowResize = () => { if (activeRef.current) remeasure() }
    // Switching windows or apps ends the reading; nothing would end it later
    // while the pointer is parked over the sidebar.
    const onWindowBlur = () => { if (activeRef.current) stop() }
    root.addEventListener('wheel', onScrollIntent, { passive: true })
    root.addEventListener('touchmove', onScrollIntent, { passive: true })
    root.addEventListener('keydown', onScrollIntent)
    root.addEventListener('scroll', onLaneScroll, { capture: true, passive: true })
    document.addEventListener('pointerdown', onPress, true)
    document.addEventListener('keydown', onAnyKey, true)
    root.addEventListener('pointerenter', onEnter)
    root.addEventListener('pointerleave', onLeave)
    root.addEventListener('pointermove', onMove)
    window.addEventListener('resize', onWindowResize)
    window.addEventListener('blur', onWindowBlur)
    return () => {
      root.removeEventListener('wheel', onScrollIntent)
      root.removeEventListener('touchmove', onScrollIntent)
      root.removeEventListener('keydown', onScrollIntent)
      root.removeEventListener('scroll', onLaneScroll, { capture: true })
      document.removeEventListener('pointerdown', onPress, true)
      document.removeEventListener('keydown', onAnyKey, true)
      root.removeEventListener('pointerenter', onEnter)
      root.removeEventListener('pointerleave', onLeave)
      root.removeEventListener('pointermove', onMove)
      window.removeEventListener('resize', onWindowResize)
      window.removeEventListener('blur', onWindowBlur)
      stop()
      rowObserverRef.current = null
    }
  }, [enabled, rootRef, laneRef, stop, scheduleStop, touchIdle])

  return titles
}

/** Settings → Chat → Sessions → Show Full Titles While Scrolling, read live
 *  through the same `mc-config-changed` event every chat setting broadcasts. */
export function useFullTitlesOnScrollSetting(): boolean {
  const [on, setOn] = useState(() => loadChatConfig().fullTitlesOnScroll)
  useEffect(() => {
    const onChange = () => setOn(loadChatConfig().fullTitlesOnScroll)
    window.addEventListener('mc-config-changed', onChange)
    return () => window.removeEventListener('mc-config-changed', onChange)
  }, [])
  return on
}
