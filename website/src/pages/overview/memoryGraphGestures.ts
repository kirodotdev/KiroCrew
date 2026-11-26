/**
 * Gestures on the memory graph, on the Figma model:
 *
 * - a two-finger swipe or a mouse wheel pans;
 * - a trackpad pinch (a wheel with ctrlKey) or Ctrl/Cmd + wheel zooms around the pointer;
 * - press and drag on empty canvas draws a selection box;
 * - Space + drag, or a middle-button drag, pans (the cursor shows a grab hand);
 * - on a touch screen, one finger pans and two fingers pinch to zoom (and pan
 *   with their midpoint). Touch has no box select; a tap still clicks.
 *
 * The canvas's own pan and zoom are off, so nothing else reacts. Every wheel is a
 * non-passive listener that calls preventDefault, so the page never scrolls under
 * the graph.
 */
export type Point = { x: number; y: number }
/** What the gestures move: the view, by screen pixels. */
export type GestureTarget = {
  /** Slide the content by `delta` screen pixels the other way (a scroll). */
  panBy(delta: Point): void
  /** Zoom by `factor` (above 1 zooms out), keeping the point under `at` in place. */
  zoomAt(at: Point, factor: number): void
}
export type Box = { x: number; y: number; width: number; height: number }

const ZOOM_SPEED = 0.01
/** Largest wheel delta one event zooms by: e^(40 * 0.01), about 1.5x. */
const MAX_WHEEL_STEP = 40
/** A press that moves less than this is a click, not a box. */
const DRAG_SLOP = 4

const rectOf = (a: Point, b: Point): Box => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) })

function pixels(event: WheelEvent, height: number): Point {
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1
  return { x: event.deltaX * scale, y: event.deltaY * scale }
}

export type GestureHandlers = {
  /** Whether a node is under the pointer (a press there is a node click, not a box). */
  overNode: () => boolean
  onBox: (box: Box | null) => void
  onSelect: (box: Box) => void
  onClear: () => void
}

/** Wire the gestures to `container`; returns cleanup and gesture cancellation. */
export function attachGraphGestures(container: HTMLElement, target: GestureTarget, handlers: GestureHandlers): { detach: () => void; cancel: () => void } {
  let space = false
  let drag: { mode: 'pan' | 'box'; start: Point; last: Point; moved: boolean; pointerId: number } | null = null
  const touches = new Map<number, Point>()
  // Inside the container's border: the content box is where the canvas draws.
  const local = (event: MouseEvent): Point => { const r = container.getBoundingClientRect(); return { x: event.clientX - r.left - container.clientLeft, y: event.clientY - r.top - container.clientTop } }
  const cursor = () => { container.style.cursor = drag?.mode === 'pan' ? 'grabbing' : space ? 'grab' : '' }

  const wheel = (event: WheelEvent) => {
    event.preventDefault()
    const delta = pixels(event, container.clientHeight)
    // A mouse wheel notch (100+) zooms 1.5x at most; a pinch's small steps are unchanged.
    if (event.ctrlKey || event.metaKey) target.zoomAt(local(event), Math.exp(Math.max(-MAX_WHEEL_STEP, Math.min(MAX_WHEEL_STEP, delta.y)) * ZOOM_SPEED))
    else target.panBy(delta)
  }
  const down = (event: PointerEvent) => {
    if (event.pointerType === 'touch') { touches.set(event.pointerId, local(event)); return }
    const pan = event.button === 1 || (event.button === 0 && space)
    if (!pan && (event.button !== 0 || handlers.overNode())) return
    event.preventDefault()
    const at = local(event)
    drag = { mode: pan ? 'pan' : 'box', start: at, last: at, moved: false, pointerId: event.pointerId }
    container.setPointerCapture?.(event.pointerId)
    if (!pan) handlers.onBox(rectOf(at, at))
    cursor()
  }
  const touchMove = (event: PointerEvent) => {
    const before = touches.get(event.pointerId)
    if (!before) return
    const at = local(event)
    if (touches.size === 1) {
      target.panBy({ x: before.x - at.x, y: before.y - at.y })
    } else if (touches.size === 2) {
      const [other] = [...touches.entries()].filter(([id]) => id !== event.pointerId).map(([, p]) => p)
      const was = Math.hypot(before.x - other.x, before.y - other.y)
      const now = Math.hypot(at.x - other.x, at.y - other.y)
      const midBefore = { x: (before.x + other.x) / 2, y: (before.y + other.y) / 2 }
      const midNow = { x: (at.x + other.x) / 2, y: (at.y + other.y) / 2 }
      if (was > 0 && now > 0) target.zoomAt(midNow, was / now)
      target.panBy({ x: midBefore.x - midNow.x, y: midBefore.y - midNow.y })
    }
    touches.set(event.pointerId, at)
  }
  const move = (event: PointerEvent) => {
    if (event.pointerType === 'touch') { touchMove(event); return }
    if (!drag) return
    const at = local(event)
    if (!drag.moved && Math.hypot(at.x - drag.start.x, at.y - drag.start.y) < DRAG_SLOP) return
    drag.moved = true
    if (drag.mode === 'pan') target.panBy({ x: drag.last.x - at.x, y: drag.last.y - at.y })
    else handlers.onBox(rectOf(drag.start, at))
    drag.last = at
  }
  const up = (event: PointerEvent) => {
    if (event.pointerType === 'touch') { touches.delete(event.pointerId); return }
    if (!drag) return
    const done = drag
    drag = null
    container.releasePointerCapture?.(event.pointerId)
    cursor()
    if (done.mode !== 'box') return
    handlers.onBox(null)
    // A plain click on empty canvas clears the selection.
    if (done.moved) handlers.onSelect(rectOf(done.start, local(event)))
    else handlers.onClear()
  }
  const cancel = () => {
    const done = drag
    drag = null
    if (done) container.releasePointerCapture?.(done.pointerId)
    touches.clear()
    cursor()
    handlers.onBox(null)
  }
  const editing = (el: EventTarget | null) => el instanceof HTMLElement && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))
  const keydown = (event: KeyboardEvent) => {
    if (event.code !== 'Space' || editing(event.target) || !container.isConnected || container.offsetParent === null) return
    // Space on a focused control elsewhere on the page keeps pressing that control.
    if (event.target instanceof Element && event.target !== document.body && !container.contains(event.target)) return
    event.preventDefault()
    if (!space) { space = true; cursor() }
  }
  const keyup = (event: KeyboardEvent) => { if (event.code === 'Space') { space = false; cursor() } }
  const middle = (event: MouseEvent) => { if (event.button === 1) event.preventDefault() }

  // Without this the browser takes a touch pan or pinch and fires pointercancel.
  const touchAction = container.style.touchAction
  container.style.touchAction = 'none'
  container.addEventListener('wheel', wheel, { passive: false })
  container.addEventListener('pointerdown', down, { capture: true })
  container.addEventListener('mousedown', middle)
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  window.addEventListener('pointercancel', up)
  window.addEventListener('keydown', keydown)
  window.addEventListener('keyup', keyup)
  const detach = () => {
    container.removeEventListener('wheel', wheel)
    container.removeEventListener('pointerdown', down, { capture: true })
    container.removeEventListener('mousedown', middle)
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    window.removeEventListener('pointercancel', up)
    window.removeEventListener('keydown', keydown)
    window.removeEventListener('keyup', keyup)
    container.style.touchAction = touchAction
  }
  return { detach, cancel }
}
