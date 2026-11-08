/**
 * Header drag-gap geometry — the horizontal spans of the 42px title-bar band
 * that carry NO interactive control, so an Electron host can mark exactly those
 * as `-webkit-app-region: drag`.
 *
 * Why this exists: a remote-instance pane is a cross-origin <iframe>. Electron's
 * injected drag bar and its `no-drag` blanket only apply to the host's MAIN
 * frame, and the whole iframe is in that blanket — so the host subtracts the
 * entire top band and the pane's header cannot move the window. Draggable
 * regions do not descend into a subframe, so the pane cannot mark itself
 * draggable either. The fix relays this geometry to the host, which re-adds
 * drag ONLY in the gaps between the pane's own controls.
 *
 * The result is failure-safe by construction: each control's box is grown
 * outward by CONTROL_PAD_PX before it is excluded, so a stale or rounded
 * measurement can only shrink a gap (a small non-draggable sliver), never let a
 * drag strip straddle a control and swallow its clicks.
 */
export interface DragGap {
  /** Left edge in viewport pixels. */
  x: number
  /** Width in pixels. */
  w: number
}

/**
 * Interactive controls that must stay clickable. Mirrors the `no-drag` blanket
 * injected by electron/main.js so the pane's draggable area matches the local
 * header's exactly. `iframe` is omitted — the header carries none.
 */
const CONTROL_SEL = 'a,button,input,select,textarea,[role="button"]'
const INTERACTIVE_SEL = [CONTROL_SEL, '[tabindex]'].join(',')
/**
 * Pointer surfaces of other layers that no control selector matches: a
 * floating panel moved by its own header (`role="dialog"`) and a resize grip
 * (`role="separator"`). A strip over one would turn its drag into a window move.
 */
const BAND_SURFACE_SEL = '[role="dialog"],[role="alertdialog"],[role="separator"]'
/** Outward padding on each control box; the safety margin that keeps gaps off controls. */
const CONTROL_PAD_PX = 6
/** Gaps narrower than this are dropped: not worth a region, and rounding-risky. */
const MIN_GAP_PX = 10

/**
 * Compute the drag gaps of `header` within the `[0, bandWidth]` viewport band.
 * Returns an empty list when the header is not laid out (hidden pane) or the
 * band has no width.
 */
export function computeHeaderDragGaps(header: Element, bandWidth: number): DragGap[] {
  return gapsAround(Array.from(header.querySelectorAll(INTERACTIVE_SEL)) as HTMLElement[], bandWidth)
}

/** Height of the title-bar band the drag strips cover (`.host-drag-strip`). */
export const TITLE_BAND_PX = 42

/**
 * Drag gaps of the local title-bar band, for strips painted after the whole
 * app root. Electron unions app-region rectangles in document order, so such a
 * strip turns ANY control under it into a window drag, not just the header's:
 * the phone nav drawer, the image viewer's toolbar, an in-place notice or
 * popover drawn across the band. So the gaps avoid every control, dialog and
 * resize grip whose box reaches into the band, wherever it lives in the document.
 *
 * One kind is left out, because it is what the strips exist to cover: content
 * of the `#main-content` scroller scrolled up past its top edge. It is clipped
 * out of sight there, yet its layout box still cancels the injected drag bar.
 * A control inside the scroller that sits in a `position: fixed` layer (an
 * expanded widget, a popover) is not clipped, so it still counts. Anything not
 * shown to be clipped counts, so a new surface can only shrink the gaps.
 *
 * Containers that `[tabindex]` matches but that are not controls are left out
 * too, because their box spans the band without being clickable there: an
 * ancestor of the header or of `#main-content` (the shell itself), and a
 * script-only focus container, `tabindex="-1"` with no `role` that holds
 * controls of its own (the main landmark, the transcript scroller). In focus
 * mode `#main-content` starts inside the band, so counting it would block the
 * whole content column. Their controls are matched on their own and still
 * count. A `tabindex="-1"` element with a role (an option, a dialog) or with
 * no controls inside (a clickable row, an iframe, a heading) still counts:
 * nothing in the DOM says its own area is not a click target.
 */
export function computeTitleBandDragGaps(
  header: Element, bandWidth: number, doc: Document = document, headerOverlaysMain = false,
): DragGap[] {
  const main = doc.getElementById('main-content')
  const mainTop = main ? main.getBoundingClientRect().top : 0
  const controls = (Array.from(doc.querySelectorAll(`${INTERACTIVE_SEL},${BAND_SURFACE_SEL}`)) as HTMLElement[]).filter(el => {
    if (header.contains(el)) return true
    if (el.contains(header) || (main && el.contains(main))) return false
    const r = el.getBoundingClientRect()
    if (r.top >= TITLE_BAND_PX || r.bottom <= 0 || isFocusContainer(el)) return false
    // In the content column and not in a fixed layer: hidden under the peeked
    // header when it overlays the column (focus mode), else clipped by the
    // scroller, where only the part from its top edge down shows.
    const inColumn = main?.contains(el) && !inFixedLayer(el, main)
    if (inColumn && headerOverlaysMain) return false
    return !(inColumn && (r.bottom <= mainTop || Math.max(r.top, mainTop) >= TITLE_BAND_PX))
  })
  return gapsAround(controls, bandWidth)
}

/** Whether `el` is a script-only focus container: `tabindex="-1"`, no role, not a control, holding controls. */
function isFocusContainer(el: Element): boolean {
  return el.getAttribute('tabindex')?.trim() === '-1' && !el.hasAttribute('role') && !el.matches(CONTROL_SEL)
    && el.querySelector(INTERACTIVE_SEL) !== null
}

/** Whether `el` or an ancestor below `stop` is `position: fixed`. */
function inFixedLayer(el: Element, stop: Element): boolean {
  for (let n: Element | null = el; n && n !== stop; n = n.parentElement) {
    if (getComputedStyle(n).position === 'fixed') return true
  }
  return false
}

function gapsAround(controls: HTMLElement[], bandWidth: number): DragGap[] {
  const width = Math.max(0, Math.floor(bandWidth))
  if (width === 0) return []
  const blocked: Array<[number, number]> = []
  for (const el of controls) {
    const r = el.getBoundingClientRect()
    // Skip controls with no box (display:none, or a not-yet-laid-out pane).
    if (r.width <= 0 || r.height <= 0) continue
    const left = Math.max(0, Math.floor(r.left - CONTROL_PAD_PX))
    const right = Math.min(width, Math.ceil(r.right + CONTROL_PAD_PX))
    if (right > left) blocked.push([left, right])
  }
  // Merge overlapping / touching blocked intervals so the complement is clean.
  blocked.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = []
  for (const [l, r] of blocked) {
    const last = merged[merged.length - 1]
    if (last && l <= last[1]) last[1] = Math.max(last[1], r)
    else merged.push([l, r])
  }
  // The complement within [0, width] is the set of drag gaps.
  const gaps: DragGap[] = []
  let cursor = 0
  for (const [l, r] of merged) {
    if (l - cursor >= MIN_GAP_PX) gaps.push({ x: cursor, w: l - cursor })
    cursor = Math.max(cursor, r)
  }
  if (width - cursor >= MIN_GAP_PX) gaps.push({ x: cursor, w: width - cursor })
  return gaps
}

/**
 * Call `onChange` on every reflow that can move a control in `header`: a window
 * or zoom resize, a child resizing (a badge widening a cluster leaves the header
 * itself the same size, so observing the header alone would miss it), and any
 * child, class or style change in its subtree. Returns the unsubscribe.
 *
 * Drag gaps are only failure-safe while they are fresh: a gap measured before a
 * control appeared can sit over that control and turn its clicks into window
 * drags, so every surface that keeps gaps on screen re-measures through this.
 */
export function observeHeaderLayout(header: Element, onChange: () => void): () => void {
  const ro = new ResizeObserver(onChange)
  ro.observe(header)
  for (const c of Array.from(header.children)) ro.observe(c)
  const mo = new MutationObserver(onChange)
  mo.observe(header, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] })
  window.addEventListener('resize', onChange)
  return () => {
    ro.disconnect()
    mo.disconnect()
    window.removeEventListener('resize', onChange)
  }
}

/**
 * Call `onChange` at most once per animation frame after any change in `doc`
 * that can open, close or move a surface in the title-bar band: a node added
 * or removed anywhere, a class, style or `hidden` change, or the end of a CSS
 * transition or animation (a class flip starts a slide; the surface lands
 * later). A streaming transcript commits on every token, so the scan runs once
 * per frame, not once per commit. Returns the unsubscribe.
 */
export function observeDocumentLayout(onChange: () => void, doc: Document = document): () => void {
  let frame = 0
  const schedule = () => {
    if (frame) return
    frame = requestAnimationFrame(() => { frame = 0; onChange() })
  }
  const mo = new MutationObserver(schedule)
  // From <html>, whose inline style carries the Windows caption reserve.
  mo.observe(doc.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] })
  doc.addEventListener('transitionend', schedule, true)
  doc.addEventListener('animationend', schedule, true)
  // Focus-only styles (a skip link shown on :focus) move boxes without any
  // mutation, so a focus change re-measures too.
  doc.addEventListener('focusin', schedule, true)
  doc.addEventListener('focusout', schedule, true)
  return () => {
    mo.disconnect()
    doc.removeEventListener('transitionend', schedule, true)
    doc.removeEventListener('animationend', schedule, true)
    doc.removeEventListener('focusin', schedule, true)
    doc.removeEventListener('focusout', schedule, true)
    if (frame) cancelAnimationFrame(frame)
  }
}
