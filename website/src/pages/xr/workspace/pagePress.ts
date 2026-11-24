/**
 * What a pointer on a page means to the page: it moves the page's own pointer, a press that stays put is a click,
 * and a press that is dragged scrolls.
 *
 * The panel feeds this with where each pointer is on the page, in CSS pixels. It answers with the events the page's
 * source is to get (`source.ts`). One of these per panel; it keeps one press per pointer.
 */
import { PAGES } from './layout'
import type { PagePointer, PageScroll } from './source'

export type PageEvent = { pointer: PagePointer } | { scroll: PageScroll }

interface Press { x0: number; y0: number; x: number; y: number; scrolling: boolean }

export class PagePress {
  private readonly presses = new Map<number, Press>()

  /** Whether this pointer holds a press on the page. */
  held(pointerId: number): boolean { return this.presses.has(pointerId) }

  move(pointerId: number, x: number, y: number): PageEvent[] {
    const p = this.presses.get(pointerId)
    if (!p) return [{ pointer: { type: 'move', pointerId, x, y } }]
    const out: PageEvent[] = []
    if (!p.scrolling && Math.hypot(x - p.x0, y - p.y0) > PAGES.scrollStartPx) {
      // The page under the press moves from here on, so the press can no longer be a click.
      p.scrolling = true
      out.push({ pointer: { type: 'cancel', pointerId, x, y } })
      // The scroll counts from where the press began, so the page does not lag the hand by the start of the drag.
      p.x = p.x0
      p.y = p.y0
    }
    // The page follows the hand: dragged up, it scrolls down. The scroll stays on what lay under the press when it
    // began, as a browser keeps a touch's scroll on the element it started on: sent where the hand is now, a drag
    // that left its list went on scrolling nothing (the eighth pass: the last half second of a finger's drag sent
    // its wheels onto the chat's text field and moved no picture).
    if (p.scrolling) out.push({ scroll: { x: p.x0, y: p.y0, dx: p.x - x, dy: p.y - y } })
    else out.push({ pointer: { type: 'move', pointerId, x, y } })
    p.x = x
    p.y = y
    return out
  }

  down(pointerId: number, x: number, y: number): PageEvent[] {
    this.presses.set(pointerId, { x0: x, y0: y, x, y, scrolling: false })
    return [{ pointer: { type: 'down', pointerId, x, y } }]
  }

  up(pointerId: number, x: number, y: number): PageEvent[] {
    const p = this.presses.get(pointerId)
    this.presses.delete(pointerId)
    // A press that became a scroll was cancelled when it did: the page hears no release for it.
    return p && !p.scrolling ? [{ pointer: { type: 'up', pointerId, x, y } }] : []
  }

  cancel(pointerId: number, x: number, y: number): PageEvent[] {
    const p = this.presses.get(pointerId)
    this.presses.delete(pointerId)
    return p && !p.scrolling ? [{ pointer: { type: 'cancel', pointerId, x, y } }] : []
  }

  leave(pointerId: number, x: number, y: number): PageEvent[] {
    // A pointer that holds a press is still the page's: it only left the rectangle.
    return this.presses.has(pointerId) ? [] : [{ pointer: { type: 'leave', pointerId, x, y } }]
  }
}
