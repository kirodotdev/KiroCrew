import { useEffect, useRef, useState, type RefObject } from 'react'
import type { IMarker, Terminal } from '@xterm/xterm'

/**
 * Touch scrolling and text selection for the terminal on phones and tablets.
 *
 * WHY THIS EXISTS. xterm's selection is driven by mouse events and never starts
 * from touch, and its own touch scrolling moves the viewport only while the
 * finger is down: a flick stops dead on lift. This hook gives a touch device the
 * two gestures a phone user expects from any scrolling text:
 *
 *   • SCROLL WITH MOMENTUM. The hook moves `.xterm-viewport` with the finger,
 *     samples its speed, and after the lift keeps scrolling with a decaying
 *     velocity until it slows to a stop, hits an end, or the next touch lands.
 *     A full-screen app (the alternate screen, or mouse tracking on) has no
 *     scrollback, so there the drag becomes wheel input, which xterm turns
 *     into arrow keys or mouse-wheel reports, and nothing glides.
 *   • LONG-PRESS TO SELECT, DRAG HANDLES TO ADJUST. A still press selects the
 *     word under the finger (one cell on a blank). Keeping the finger down and
 *     dragging extends it. Two handles then sit at the selection's ends; drag
 *     either one to move that end a character at a time. The key bar's Copy key
 *     reads term.getSelection() unchanged, so it copies exactly this range.
 *
 * WHY NATIVE CAPTURE LISTENERS. xterm binds touchstart on its own element and
 * stops its propagation, so a React onTouchStart on an ancestor never runs on a
 * real device. The listeners here are bound in the CAPTURE phase on the host
 * that wraps the terminal, so they see each touch before xterm does, and
 * non-passive, so a drag can preventDefault the page scroll. A move the hook
 * acts on (a scroll, a handle drag, the drag after a long-press) stops there,
 * so xterm's own touch scroll does not move the viewport a second time.
 *
 * STALENESS. Every geometry read is gated on term.element?.offsetParent: a tab
 * switch / remote-instance switch / closed pane sets display:none on the host
 * (offsetParent === null), and a long-press timer or a fling frame that fires
 * against a hidden terminal must not touch a pane the user cannot see.
 */

/** How long a touch must stay roughly still before it selects. 500ms matches
 *  the platform long-press convention (Android/iOS text selection). */
const LONG_PRESS_MS = 500
/** A touch that drifts more than this many CSS px before the timer fires is a
 *  scroll, not a long-press. */
const MOVE_TOLERANCE_PX = 10
/** Finger samples older than this at lift do not count toward the fling speed. */
const VELOCITY_WINDOW_MS = 100
/** Slower than this at lift (px/ms) and the viewport just stops. */
const FLING_MIN_VELOCITY = 0.1
/** The fling ends once it decays below this (px/ms). */
const FLING_STOP_VELOCITY = 0.02
/** Velocity kept per 16ms frame; about 1s of glide from a brisk flick. */
const FLING_FRICTION = 0.95
/** A frame gap past this ends a glide (the tab was hidden or the page stalled). */
const FLING_MAX_FRAME_GAP_MS = 100

/** The attribute that marks a handle's touch target; the CliPanel layer and the
 *  hook agree on it so a touch on a handle is recognised by the same listener. */
export const TOUCH_HANDLE_ATTR = 'data-terminal-touch-handle'

export interface CellPoint { col: number; row: number }

/**
 * Map a client point to the terminal cell under it: a column, and an ABSOLUTE
 * buffer row (scrollback + viewport), or null when geometry is unreadable.
 * Pure so the math is unit-testable without a canvas; xterm has no point→cell
 * API. `screenRect` is the bounding rect of `.xterm-screen` (the grid, without
 * the scrollbar). Both axes clamp to the grid.
 */
export function clientPointToCell(
  clientX: number,
  clientY: number,
  screenRect: { left: number; top: number; width: number; height: number },
  cols: number,
  rows: number,
  viewportY: number,
): CellPoint | null {
  if (cols <= 0 || rows <= 0 || !(screenRect.width > 0) || !(screenRect.height > 0)) return null
  const col = Math.floor((clientX - screenRect.left) / (screenRect.width / cols))
  const row = Math.floor((clientY - screenRect.top) / (screenRect.height / rows))
  return {
    col: Math.max(0, Math.min(col, cols - 1)),
    row: viewportY + Math.max(0, Math.min(row, rows - 1)),
  }
}

/**
 * The word around `col` on buffer row `row`, as [startCol, endCol) on that row.
 * A word is a run of non-blank cells; the right half of a wide (CJK) character
 * has width 0 and belongs to the character before it. A blank cell selects
 * itself, so a long-press always produces a selection to adjust.
 * shortcut: stays on one row; a word that wraps onto the next row is cut at the
 * edge, and the handles extend it.
 */
export function wordBoundsAt(term: Terminal, col: number, row: number): [number, number] {
  const line = term.buffer?.active?.getLine(row)
  const cols = term.cols ?? 0
  const isWordCell = (x: number): boolean => {
    const cell = line?.getCell(x)
    if (!cell) return false
    if (cell.getWidth() === 0) return true
    const ch = cell.getChars()
    return ch !== '' && ch !== ' '
  }
  if (!isWordCell(col)) return [col, col + 1]
  let start = col
  while (start > 0 && isWordCell(start - 1)) start--
  let end = col + 1
  while (end < cols && isWordCell(end)) end++
  return [start, end]
}

/** Velocity (px/ms, positive = scroll toward newer output) from finger samples
 *  taken during a drag, or 0 when the finger had paused before lifting. Pure. */
export function flingVelocity(samples: { t: number; y: number }[], now: number): number {
  const recent = samples.filter(s => now - s.t <= VELOCITY_WINDOW_MS)
  if (recent.length < 2) return 0
  const first = recent[0]
  const last = recent[recent.length - 1]
  const dt = last.t - first.t
  if (dt <= 0) return 0
  // Finger moving UP (y shrinking) scrolls DOWN the buffer, as xterm's drag does.
  return (first.y - last.y) / dt
}

/** One handle's place, in px relative to the host element. `height` is a cell. */
/** `flip` puts the knob on the inner side: the start on the first visible row,
 *  the end on the last, where the usual side would fall outside the grid. */
export interface HandlePos { left: number; top: number; height: number; flip?: boolean }
/** Both ends of the current selection; an end scrolled out of view is null. */
export interface TouchSelectionHandles { start: HandlePos | null; end: HandlePos | null }

type Drag = {
  /** The selection always spans [min(lo, finger), max(hi, finger)] in linear
   *  cell indices (row * cols + col): lo === hi for a handle drag (the other
   *  end), the long-pressed word's bounds for the drag right after it. */
  lo: number
  hi: number
  /** Finger offset from the dragged end's cell centre at grab time, so the end
   *  follows the finger instead of jumping under it (the knob sits off-line). */
  dx: number
  dy: number
}

function sameHandles(a: TouchSelectionHandles | null, b: TouchSelectionHandles | null): boolean {
  if (a === b) return true
  if (!a || !b) return false
  const eq = (p: HandlePos | null, q: HandlePos | null) =>
    p === q || (!!p && !!q && p.left === q.left && p.top === q.top && p.height === q.height && !p.flip === !q.flip)
  return eq(a.start, b.start) && eq(a.end, b.end)
}

export interface TerminalTouchSelection {
  /** Where to draw the two selection handles, or null with no selection. */
  handles: TouchSelectionHandles | null
}

/**
 * Wire touch scrolling and selection onto a terminal. `hostRef` is the element
 * that wraps the terminal and the handles (the positioning context the handles
 * are drawn in). `enabled` is the caller's touch-device gate (useIsTouchDevice):
 * on a mouse device no listener is bound, so xterm's own mouse selection, the
 * wheel and the keyboard are untouched.
 */
export function useTerminalTouchSelection(
  term: Terminal,
  enabled: boolean,
  hostRef: RefObject<HTMLElement | null>,
): TerminalTouchSelection {
  const [handles, setHandles] = useState<TouchSelectionHandles | null>(null)
  const handlesRef = useRef<TouchSelectionHandles | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!enabled || !host) {
      handlesRef.current = null
      setHandles(null)
      return
    }

    const visible = () => !!term.element?.offsetParent
    const screenEl = (): HTMLElement | null =>
      (term.element?.querySelector?.('.xterm-screen') as HTMLElement | null) ?? term.element ?? null
    const viewportEl = (): HTMLElement | null =>
      (term.element?.querySelector?.('.xterm-viewport') as HTMLElement | null) ?? null
    const viewportY = () => term.buffer?.active?.viewportY ?? 0
    const cols = () => term.cols ?? 0

    const cellAt = (clientX: number, clientY: number): CellPoint | null => {
      const screen = screenEl()
      if (!screen || !visible()) return null
      const r = screen.getBoundingClientRect()
      return clientPointToCell(clientX, clientY, r, cols(), term.rows ?? 0, viewportY())
    }
    const linear = (p: CellPoint) => p.row * cols() + p.col
    /** Select the inclusive cell range between two linear indices. */
    const selectSpan = (a: number, b: number) => {
      const c = cols()
      if (c <= 0) return
      const s = Math.min(a, b)
      const e = Math.max(a, b)
      term.select(s % c, Math.floor(s / c), e - s + 1)
    }

    // ── handle layout ──
    const layout = () => {
      let next: TouchSelectionHandles | null = null
      const pos = term.getSelectionPosition?.()
      const screen = screenEl()
      if (pos && screen && visible()) {
        const sr = screen.getBoundingClientRect()
        const hr = host.getBoundingClientRect()
        const rows = term.rows ?? 0
        const cw = cols() > 0 ? sr.width / cols() : 0
        const ch = rows > 0 ? sr.height / rows : 0
        const vy = viewportY()
        const place = (x: number, y: number, edgeRow: number): HandlePos | null => {
          const r = y - vy
          if (r < 0 || r >= rows || !(ch > 0)) return null
          const at = { left: sr.left - hr.left + x * cw, top: sr.top - hr.top + r * ch, height: ch }
          return r === edgeRow ? { ...at, flip: true } : at
        }
        // getSelectionPosition's end is exclusive: the caret after the last cell.
        next = { start: place(pos.start.x, pos.start.y, 0), end: place(pos.end.x, pos.end.y, rows - 1) }
      }
      if (sameHandles(next, handlesRef.current)) return
      handlesRef.current = next
      setHandles(next)
    }

    // ── momentum ──
    let flingFrame = 0
    const stopFling = () => {
      if (flingFrame) cancelAnimationFrame(flingFrame)
      flingFrame = 0
    }
    const startFling = (v0: number) => {
      stopFling()
      const vp = viewportEl()
      if (!vp) return
      let v = v0
      let pos = vp.scrollTop
      let last = performance.now()
      const step = (now: number) => {
        flingFrame = 0
        if (!visible() || document.hidden) return
        const dt = Math.max(0, now - last)
        // A long gap is a backgrounded tab, not a frame: stop rather than jump.
        if (dt > FLING_MAX_FRAME_GAP_MS) return
        last = now
        pos += v * dt
        vp.scrollTop = pos
        // The browser clamps scrollTop at either end: a gap means we hit one.
        if (Math.abs(vp.scrollTop - pos) > 1) return
        v *= Math.pow(FLING_FRICTION, dt / 16)
        if (Math.abs(v) < FLING_STOP_VELOCITY) return
        flingFrame = requestAnimationFrame(step)
      }
      flingFrame = requestAnimationFrame(step)
    }

    // ── full-screen apps ──
    // A pager or TUI on the alternate screen has no scrollback, and an app
    // with mouse tracking reads the wheel itself. Either way a drag becomes
    // wheel input to xterm, which sends arrow keys or mouse-wheel reports.
    const wheelMode = () =>
      term.buffer?.active?.type === 'alternate' || (term.modes?.mouseTrackingMode ?? 'none') !== 'none'
    let wheelRest = 0
    const wheel = (dy: number, clientX: number, clientY: number) => {
      const target = screenEl()
      const rows = term.rows ?? 0
      if (!target || rows <= 0) return
      const ch = target.getBoundingClientRect().height / rows
      if (!(ch > 0)) return
      // One wheel step per row of travel; the rest carries to the next move.
      wheelRest += dy
      while (Math.abs(wheelRest) >= ch) {
        const step = Math.sign(wheelRest) * ch
        wheelRest -= step
        target.dispatchEvent(new WheelEvent('wheel', { deltaY: step, deltaMode: 0, clientX, clientY, bubbles: true, cancelable: true }))
      }
    }

    // ── gesture state ──
    let pressTimer: ReturnType<typeof setTimeout> | undefined
    let press: { x: number; y: number; cell: CellPoint } | null = null
    let scrolling = false
    let samples: { t: number; y: number }[] = []
    let drag: Drag | null = null
    let longPressFired = false
    let stoppedFling = false
    /** True from a touchstart on the grid or a handle until its touchend; the
     *  contextmenu that Android fires on a long-press is suppressed meanwhile. */
    let touchActive = false
    let lastY = 0
    // A marker on the gesture's anchor row, and the column count its cell
    // indices were built with. Panning leaves `line` where it was; new output
    // that trims scrollback moves it (or disposes it), and a refit to another
    // width reflows the rows. Either way every cell the gesture holds names
    // different text.
    let anchor: { marker: IMarker; row: number; cols: number } | null = null
    const unpin = () => { anchor?.marker.dispose?.(); anchor = null }
    const pin = (row: number) => {
      unpin()
      const a = term.buffer?.active
      const marker = a && term.registerMarker?.(row - ((a.baseY ?? 0) + (a.cursorY ?? 0)))
      if (marker) anchor = { marker, row, cols: cols() }
    }
    const cancelPress = () => { clearTimeout(pressTimer); pressTimer = undefined; press = null }
    const dropStaleGesture = () => {
      if (anchor && (anchor.marker.isDisposed || anchor.marker.line !== anchor.row || anchor.cols !== cols())) {
        // The rows moved under the gesture: drop it, and the selection it built.
        const hadDrag = drag !== null
        cancelPress()
        drag = null
        unpin()
        if (hadDrag) term.clearSelection?.()
      }
      layout()
    }
    // xterm's DOM renderer replaces row spans as it repaints (the cursor blink is
    // enough), and a touch keeps targeting the node it started on. Once that span
    // is detached its touchmove/touchend no longer bubble to the host, so the
    // gesture also listens on the start target itself. `seen` keeps an event
    // that still bubbles from being handled twice.
    const seen = new WeakSet<Event>()
    let followed: EventTarget | null = null
    const unfollow = () => {
      if (!followed) return
      followed.removeEventListener('touchmove', onTouchMove as EventListener)
      followed.removeEventListener('touchend', onTouchEnd as EventListener)
      followed.removeEventListener('touchcancel', onTouchEnd as EventListener)
      followed = null
    }
    const follow = (target: EventTarget | null) => {
      unfollow()
      if (!target || target === host) return
      followed = target
      target.addEventListener('touchmove', onTouchMove as EventListener, { passive: false })
      target.addEventListener('touchend', onTouchEnd as EventListener, { passive: false })
      target.addEventListener('touchcancel', onTouchEnd as EventListener, { passive: false })
    }

    const handleOf = (target: EventTarget | null): 'start' | 'end' | null => {
      const el = (target as Element | null)?.closest?.(`[${TOUCH_HANDLE_ATTR}]`)
      const v = el?.getAttribute(TOUCH_HANDLE_ATTR)
      return v === 'start' || v === 'end' ? v : null
    }
    const onScreen = (target: EventTarget | null): boolean => {
      const s = screenEl()
      return !!s && !!target && s.contains(target as Node)
    }

    const onTouchStart = (e: TouchEvent) => {
      const hadFling = flingFrame !== 0
      stopFling()
      cancelPress()
      drag = null
      unpin()
      scrolling = false
      longPressFired = false
      stoppedFling = false
      samples = []
      if (e.touches.length !== 1) return // pinch: never ours
      const t = e.touches[0]
      lastY = t.clientY
      const which = handleOf(e.target)
      if (which) {
        const pos = term.getSelectionPosition?.()
        const screen = screenEl()
        if (!pos || !screen || !visible()) return
        const c = cols()
        // The other end stays put: the last selected cell, or the first one.
        const fixed = which === 'start' ? pos.end.y * c + pos.end.x - 1 : pos.start.y * c + pos.start.x
        const sr = screen.getBoundingClientRect()
        const cw = c > 0 ? sr.width / c : 0
        const chh = (term.rows ?? 0) > 0 ? sr.height / term.rows : 0
        const endX = which === 'start' ? pos.start.x : Math.max(0, pos.end.x - 1)
        const endY = which === 'start' ? pos.start.y : pos.end.y
        drag = {
          lo: fixed,
          hi: fixed,
          dx: t.clientX - (sr.left + (endX + 0.5) * cw),
          dy: t.clientY - (sr.top + (endY - viewportY() + 0.5) * chh),
        }
        pin(Math.floor(fixed / c))
        touchActive = true
        follow(e.target)
        if (e.cancelable) e.preventDefault()
        e.stopPropagation()
        return
      }
      if (!onScreen(e.target)) return
      const cell = cellAt(t.clientX, t.clientY)
      if (!cell) return
      touchActive = true
      follow(e.target)
      stoppedFling = hadFling
      press = { x: t.clientX, y: t.clientY, cell }
      pin(cell.row)
      samples.push({ t: performance.now(), y: t.clientY })
      pressTimer = setTimeout(() => {
        const p = press
        pressTimer = undefined
        press = null
        if (!p || !visible()) return
        longPressFired = true
        const [s, eCol] = wordBoundsAt(term, p.cell.col, p.cell.row)
        const base = p.cell.row * cols()
        drag = { lo: base + s, hi: base + eCol - 1, dx: 0, dy: 0 }
        selectSpan(drag.lo, drag.hi)
        layout()
      }, LONG_PRESS_MS)
    }

    function onTouchMove(e: TouchEvent) {
      if (seen.has(e)) return
      seen.add(e)
      const t = e.touches[0]
      if (!t) return
      if (drag) {
        // A handle drag, or the drag that continues a long-press: ours alone.
        if (e.cancelable) e.preventDefault()
        e.stopPropagation()
        const cell = cellAt(t.clientX - drag.dx, t.clientY - drag.dy)
        if (!cell) return
        const f = linear(cell)
        selectSpan(Math.min(drag.lo, f), Math.max(drag.hi, f))
        layout()
        return
      }
      if (press) {
        const dx = Math.abs(t.clientX - press.x)
        const dy = Math.abs(t.clientY - press.y)
        if (dx > MOVE_TOLERANCE_PX || dy > MOVE_TOLERANCE_PX) {
          cancelPress()
          // Only a mostly vertical drag scrolls. A sideways one is left alone,
          // so an ancestor's swipe (closing the side panel) still gets it.
          scrolling = dy >= dx
        }
      }
      if (scrolling || press) {
        samples.push({ t: performance.now(), y: t.clientY })
        if (samples.length > 20) samples.shift()
      }
      // The drag scroll is done here rather than by xterm's own touchmove,
      // which goes quiet once the touched span is repainted away.
      if (scrolling) {
        const dy = lastY - t.clientY
        if (wheelMode()) wheel(dy, t.clientX, t.clientY)
        else {
          const vp = viewportEl()
          if (vp) vp.scrollTop += dy
        }
        if (e.cancelable) e.preventDefault()
        e.stopPropagation()
      }
      // While the press is pending xterm scrolls the moves itself, so each move
      // re-bases lastY: the first frame the hook scrolls adds only new travel.
      lastY = t.clientY
    }

    function onTouchEnd(e: TouchEvent) {
      if (seen.has(e)) return
      seen.add(e)
      unfollow()
      const wasDrag = drag !== null
      const wasScroll = scrolling
      const wasTapOnFling = stoppedFling && press !== null
      cancelPress()
      drag = null
      unpin()
      scrolling = false
      touchActive = false
      stoppedFling = false
      // A lift after a long-press or a handle drag would otherwise be followed
      // by the browser's emulated mousedown, which xterm reads as a click and
      // clears the selection just made. The tap that only stopped a fling is
      // swallowed for the same reason a native list does: it should not click.
      if ((wasDrag || longPressFired || wasTapOnFling) && e.cancelable) e.preventDefault()
      longPressFired = false
      // The lift still reaches ancestors: a gesture above the grid (the
      // shell's drawer swipe) resets on touchend, even one it never saw move.
      if (wasDrag) return
      if (!wasScroll || e.type === 'touchcancel') return
      // A full-screen app has no scrollback to glide through.
      if (wheelMode()) { wheelRest = 0; return }
      const v = flingVelocity(samples, performance.now())
      samples = []
      if (Math.abs(v) >= FLING_MIN_VELOCITY) startFling(v)
    }

    const onContextMenu = (e: Event) => {
      if (!touchActive) return
      e.preventDefault()
      e.stopPropagation()
    }

    const opts: AddEventListenerOptions = { capture: true, passive: false }
    host.addEventListener('touchstart', onTouchStart, opts)
    host.addEventListener('touchmove', onTouchMove, opts)
    host.addEventListener('touchend', onTouchEnd, opts)
    host.addEventListener('touchcancel', onTouchEnd, opts)
    host.addEventListener('contextmenu', onContextMenu, true)
    const disposers = [
      term.onSelectionChange?.(layout),
      term.onScroll?.(dropStaleGesture),
      term.onRender?.(layout),
      term.onResize?.(dropStaleGesture),
    ]
    layout()
    return () => {
      stopFling()
      cancelPress()
      unpin()
      unfollow()
      host.removeEventListener('touchstart', onTouchStart, opts)
      host.removeEventListener('touchmove', onTouchMove, opts)
      host.removeEventListener('touchend', onTouchEnd, opts)
      host.removeEventListener('touchcancel', onTouchEnd, opts)
      host.removeEventListener('contextmenu', onContextMenu, true)
      for (const d of disposers) d?.dispose?.()
    }
  }, [term, enabled, hostRef])

  return { handles }
}
