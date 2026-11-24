/**
 * A press, a release and a scroll handed by script into a page that the XR tab draws itself (`domSource.ts`): what
 * the room's own ray chose goes to the control under that point of the page, in the page's CSS pixels. Used when the
 * browser does not send the pointer into the page by itself: a finger on a phone, a room's thing drawn over the
 * page, and a session, where the hand's ray is no pointer of the page at all.
 *
 * What a script cannot make: the browser's own work for a real mouse. No text is selected, no context menu opens,
 * and a native list does not open on a click (`select` gets its focus only). A page's own handlers hear these events
 * (React's among them), marked as not trusted.
 */


/** What the hand did on the page: pressed, let go after a press (a click), let go of a drag, or was called off. */
export type ActKind = 'down' | 'click' | 'up' | 'cancel'

/** The element under a point of the page, or `null` (outside it, or not laid out). */
export function controlAt(doc: Document, x: number, y: number): Element | null {
  return typeof doc.elementFromPoint === 'function' ? doc.elementFromPoint(x, y) : null
}

/** The event kinds of the page's own window (a page's document has one), else this one's. */
const kinds = (doc: Document) => {
  const win = doc.defaultView as (Window & typeof globalThis) | null
  const Mouse = win?.MouseEvent ?? MouseEvent
  return { win, Mouse, Pointer: win?.PointerEvent ?? (typeof PointerEvent === 'undefined' ? Mouse : PointerEvent) }
}

/**
 * One act at a point of the page. Returns the element it went to, or `null` when nothing is there. `to`: the element
 * that took the press's down. The release, its click and a cancel go to it wherever the pointer is then, as a real
 * mouse's do; without it (or once it left the page) they go to what is under the point.
 */
export function act(doc: Document, x: number, y: number, kind: ActKind, to?: Element | null): Element | null {
  const el = kind !== 'down' && to?.isConnected ? to : controlAt(doc, x, y)
  if (!el) return null
  const { win, Mouse, Pointer } = kinds(doc)
  const mouse = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, button: 0, buttons: kind === 'down' ? 1 : 0, view: win }
  const pointer = { ...mouse, pointerId: 1, pointerType: 'mouse', isPrimary: true }
  if (kind === 'down') {
    // A real mouse's order: the press, then the focus that the press gives a field, then (at the release) the click.
    el.dispatchEvent(new Pointer('pointerdown', pointer))
    el.dispatchEvent(new Mouse('mousedown', mouse))
    // The control that takes the focus from a press.
    const field = el.closest('input, textarea, select, button, a[href], [contenteditable=""], [contenteditable="true"], [tabindex]') as HTMLElement | null
    if (field && field.ownerDocument.activeElement !== field) field.focus({ preventScroll: true })
  } else if (kind === 'cancel') {
    el.dispatchEvent(new Pointer('pointercancel', pointer))
  } else {
    el.dispatchEvent(new Pointer('pointerup', pointer))
    el.dispatchEvent(new Mouse('mouseup', mouse))
    if (kind === 'click') {
      // A click event that is dispatched runs the control's own action (a button's, a link's, a box's), as a real one.
      el.dispatchEvent(new Mouse('click', mouse))
    }
  }
  return el
}

/**
 * Scroll the part of the page under a point by `dx`, `dy` CSS pixels: the nearest box that can still scroll that way,
 * else the page. A box at its end hands the scroll on outward, as the browser's own scrolling does.
 */
export function scrollAt(doc: Document, x: number, y: number, dx: number, dy: number): Element | null {
  const view = doc.defaultView ?? window
  const scrolls = (n: Element) => {
    const s = view.getComputedStyle(n)
    const room = (at: number, most: number, by: number) => (by < 0 ? at > 0 : at < most - 1)
    return (dy !== 0 && /(auto|scroll)/.test(s.overflowY) && n.scrollHeight > n.clientHeight && room(n.scrollTop, n.scrollHeight - n.clientHeight, dy)) || (dx !== 0 && /(auto|scroll)/.test(s.overflowX) && n.scrollWidth > n.clientWidth && room(n.scrollLeft, n.scrollWidth - n.clientWidth, dx))
  }
  for (let n = controlAt(doc, x, y); n; n = n.parentElement) {
    if (scrolls(n)) { n.scrollBy({ left: dx, top: dy }); return n }
  }
  const top = doc.scrollingElement
  if (top) top.scrollBy({ left: dx, top: dy })
  return top
}
