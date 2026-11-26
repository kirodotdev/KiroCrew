/**
 * Desktop gestures on the memory graph, on the Figma model:
 *
 * - a two-finger swipe or a mouse wheel pans;
 * - a trackpad pinch (a wheel with ctrlKey) or Ctrl/Cmd + wheel zooms around the pointer;
 * - press and drag on empty canvas draws a selection box;
 * - Space + drag, or a middle-button drag, pans (the cursor shows a grab hand).
 *
 * sigma's own camera panning and zooming are off on desktop so nothing else
 * reacts. Touch keeps sigma's own handling (one finger pans, pinch zooms) and has
 * no box select. Every wheel is a non-passive listener that calls preventDefault,
 * so the page never scrolls under the graph.
 */
type Point = { x: number; y: number }
type CameraState = { x: number; y: number; ratio: number; angle: number }
/** What the gestures need from sigma. */
export type GestureSigma = {
  getCamera(): { getState(): CameraState; setState(state: Partial<CameraState>): unknown }
  viewportToFramedGraph(point: Point): Point
}
export type Box = { x: number; y: number; width: number; height: number }

const ZOOM_SPEED = 0.01
const MIN_RATIO = 0.02
const MAX_RATIO = 20
/** A press that moves less than this is a click, not a box. */
const DRAG_SLOP = 4

const rectOf = (a: Point, b: Point): Box => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) })

function pixels(event: WheelEvent, height: number): Point {
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1
  return { x: event.deltaX * scale, y: event.deltaY * scale }
}

/** Move the camera so the view slides by `delta` screen pixels. */
export function panBy(sigma: GestureSigma, delta: Point) {
  const camera = sigma.getCamera()
  const state = camera.getState()
  const origin = sigma.viewportToFramedGraph({ x: 0, y: 0 })
  const moved = sigma.viewportToFramedGraph(delta)
  camera.setState({ x: state.x + moved.x - origin.x, y: state.y + moved.y - origin.y })
}

/** Zoom by `factor` (above 1 zooms out), keeping the point under `at` in place. */
export function zoomAt(sigma: GestureSigma, at: Point, factor: number) {
  const camera = sigma.getCamera()
  const state = camera.getState()
  const ratio = Math.min(MAX_RATIO, Math.max(MIN_RATIO, state.ratio * factor))
  const fixed = sigma.viewportToFramedGraph(at)
  const k = ratio / state.ratio
  camera.setState({ ratio, x: fixed.x + (state.x - fixed.x) * k, y: fixed.y + (state.y - fixed.y) * k })
}

export type GestureHandlers = {
  /** Whether a node is under the pointer (a press there is a node click, not a box). */
  overNode: () => boolean
  onBox: (box: Box | null) => void
  onSelect: (box: Box) => void
  onClear: () => void
}

/** Wire the gestures to `container`; returns the detach function. */
export function attachGraphGestures(container: HTMLElement, sigma: GestureSigma, handlers: GestureHandlers): () => void {
  let space = false
  let drag: { mode: 'pan' | 'box'; start: Point; last: Point; moved: boolean } | null = null
  const local = (event: MouseEvent): Point => { const r = container.getBoundingClientRect(); return { x: event.clientX - r.left, y: event.clientY - r.top } }
  const cursor = () => { container.style.cursor = drag?.mode === 'pan' ? 'grabbing' : space ? 'grab' : '' }

  const wheel = (event: WheelEvent) => {
    event.preventDefault()
    const delta = pixels(event, container.clientHeight)
    if (event.ctrlKey || event.metaKey) zoomAt(sigma, local(event), Math.exp(delta.y * ZOOM_SPEED))
    else panBy(sigma, delta)
  }
  const down = (event: PointerEvent) => {
    if (event.pointerType === 'touch') return
    const pan = event.button === 1 || (event.button === 0 && space)
    if (!pan && (event.button !== 0 || handlers.overNode())) return
    event.preventDefault()
    const at = local(event)
    drag = { mode: pan ? 'pan' : 'box', start: at, last: at, moved: false }
    container.setPointerCapture?.(event.pointerId)
    cursor()
  }
  const move = (event: PointerEvent) => {
    if (!drag) return
    const at = local(event)
    if (!drag.moved && Math.hypot(at.x - drag.start.x, at.y - drag.start.y) < DRAG_SLOP) return
    drag.moved = true
    if (drag.mode === 'pan') panBy(sigma, { x: drag.last.x - at.x, y: drag.last.y - at.y })
    else handlers.onBox(rectOf(drag.start, at))
    drag.last = at
  }
  const up = (event: PointerEvent) => {
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
  const editing = (target: EventTarget | null) => target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))
  const keydown = (event: KeyboardEvent) => {
    if (event.code !== 'Space' || editing(event.target) || !container.isConnected || container.offsetParent === null) return
    // Space on a focused control elsewhere on the page keeps pressing that control.
    if (event.target instanceof Element && event.target !== document.body && !container.contains(event.target)) return
    event.preventDefault()
    if (!space) { space = true; cursor() }
  }
  const keyup = (event: KeyboardEvent) => { if (event.code === 'Space') { space = false; cursor() } }
  const middle = (event: MouseEvent) => { if (event.button === 1) event.preventDefault() }

  container.addEventListener('wheel', wheel, { passive: false })
  container.addEventListener('pointerdown', down, { capture: true })
  container.addEventListener('mousedown', middle)
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  window.addEventListener('keydown', keydown)
  window.addEventListener('keyup', keyup)
  return () => {
    container.removeEventListener('wheel', wheel)
    container.removeEventListener('pointerdown', down, { capture: true })
    container.removeEventListener('mousedown', middle)
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    window.removeEventListener('keydown', keydown)
    window.removeEventListener('keyup', keyup)
  }
}

/** The ids of `points` (screen positions inside the container) that fall in `box`. */
export function insideBox<T extends { id: string; at: Point }>(points: T[], box: Box): string[] {
  return points.filter(p => p.at.x >= box.x && p.at.x <= box.x + box.width && p.at.y >= box.y && p.at.y <= box.y + box.height).map(p => p.id)
}
