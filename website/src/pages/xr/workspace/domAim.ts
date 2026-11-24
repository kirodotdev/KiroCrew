/**
 * What a session's pointer (the dot at the picture's centre, or a raised hand's ray) lights on a page that the XR tab
 * draws itself (`domSource.ts`), and whether a pinch may press it. Read from the page's own document, in its CSS
 * pixels; `act` (`domAct.ts`) then hands the press in.
 *
 * Lit: the control that the pointer's own place lies on. Only when it lies on none does it snap: to the nearest
 * control that the page shows whole and uncovered, when that one is within `snapPx` and the second nearest is at
 * least `snapRatio` times as far. Else nothing is lit, and a pinch does nothing. A frame inside the page (a widget of
 * another page) is never lit for a pinch.
 *
 * Pressed: at the down the page is asked again. The pinch presses only the lit control, found at the same place
 * within `samePx`, that has stood lit for `stillMs`; a control that came or moved in the last `calmMs` takes none.
 */
import { controlAt } from './domAct'

export const DOM_AIM = {
  /** How long a control must stand lit before a pinch presses it (ms). */
  stillMs: 300,
  /** How far a lit control may lie from where it was lit, and still be the one that a pinch presses (CSS px). */
  samePx: 4,
  /** A control that came or moved this recently takes no pinch (ms). */
  calmMs: 1000,
  /** How near the pointer a control must be to be snapped to (CSS px): about 1.5 degrees at a page's start distance. */
  snapPx: 28,
  /** How many times as far as the nearest the second nearest control must be, for the nearest to be snapped to. */
  snapRatio: 2,
} as const

export type Rect = [number, number, number, number]

/** The control that an element is, or is inside; `null` for none and for a frame (another page inside this one). */
export function controlOf(el: Element | null): Element | null {
  if (!el || el.tagName === 'IFRAME') return null
  // What a pinch can press on a page: a control, as a person reads one (the same list as in `aimAt`).
  return el.closest('button, a[href], input, textarea, select, summary, label, [role="button"], [role="link"], [role="menuitem"], [role="option"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"], [contenteditable=""], [contenteditable="true"], [tabindex]:not([tabindex="-1"])')
}

export const rectOf = (el: Element): Rect => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height] }

/** How far a point is from a rectangle (0 inside it). */
export function distanceTo(r: Rect, x: number, y: number): number {
  const dx = Math.max(r[0] - x, 0, x - (r[0] + r[2]))
  const dy = Math.max(r[1] - y, 0, y - (r[1] + r[3]))
  return Math.hypot(dx, dy)
}

/** Whether two rectangles are the same place, within `samePx` at each side. */
export const samePlace = (a: Rect, b: Rect): boolean => a.every((v, i) => Math.abs(v - b[i]) <= DOM_AIM.samePx)

/**
 * The control to snap to from a point that lies on none: of the candidates (each with its distance), the nearest
 * when it is within `snapPx` and the second nearest is at least `snapRatio` times as far. Pure, for the rows.
 */
export function snapOf<T>(candidates: readonly { it: T; d: number }[]): T | null {
  let best: { it: T; d: number } | null = null
  let next = Infinity
  for (const c of candidates) {
    if (!best || c.d < best.d) { if (best) next = Math.min(next, best.d); best = c } else next = Math.min(next, c.d)
  }
  if (!best || best.d > DOM_AIM.snapPx) return null
  return next >= DOM_AIM.snapRatio * best.d ? best.it : null
}

/**
 * What the pointer lights at a point of the page: the control it lies on, else one it snaps to, else nothing. `sees`:
 * whether the room shows a point of the page uncovered (`roomSees.ts`); a control to snap to must be uncovered by the
 * room at its middle and its four corners too, not only by the page.
 */
export function aimAt(doc: Document, x: number, y: number, width: number, height: number, sees?: ((x: number, y: number) => boolean) | null): { el: Element; snapped: boolean } | null {
  const under = controlAt(doc, x, y)
  // A frame of the page is lit as nothing for a pinch, and nothing snaps from it.
  if (under?.tagName === 'IFRAME') return null
  const own = controlOf(under)
  if (own) return { el: own, snapped: false }
  const candidates: { it: Element; d: number }[] = []
  // The same list as in `controlOf`.
  for (const el of doc.querySelectorAll('button, a[href], input, textarea, select, summary, label, [role="button"], [role="link"], [role="menuitem"], [role="option"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"], [contenteditable=""], [contenteditable="true"], [tabindex]:not([tabindex="-1"])')) {
    const r = rectOf(el)
    if (r[2] <= 0 || r[3] <= 0) continue
    const d = distanceTo(r, x, y)
    if (d > DOM_AIM.snapPx * DOM_AIM.snapRatio) continue
    // Only a control that the page shows whole: inside the page, and nothing over its middle.
    if (r[0] < 0 || r[1] < 0 || r[0] + r[2] > width || r[1] + r[3] > height) continue
    if (controlOf(controlAt(doc, r[0] + r[2] / 2, r[1] + r[3] / 2)) !== el) continue
    if (sees && ![[r[0] + r[2] / 2, r[1] + r[3] / 2], [r[0] + 1, r[1] + 1], [r[0] + r[2] - 1, r[1] + 1], [r[0] + 1, r[1] + r[3] - 1], [r[0] + r[2] - 1, r[1] + r[3] - 1]].every(([px, py]) => sees(px, py))) continue
    candidates.push({ it: el, d })
  }
  const snap = snapOf(candidates)
  return snap ? { el: snap, snapped: true } : null
}

/** One lit control, and since when it stands lit there, and when it last moved. */
export interface Lit { el: Element; rect: Rect; snapped: boolean; since: number; movedAt: number }

/** Why a pinch at the down presses nothing, or `null` when it presses the lit control. */
export type Refusal = 'nothing-lit' | 'not-the-lit' | 'moved' | 'too-soon' | 'came' | null

export function pressFate(lit: Lit | null, now: { el: Element; rect: Rect } | null, at: number, cameAt: number | null): Refusal {
  if (!lit) return 'nothing-lit'
  if (!now || now.el !== lit.el) return 'not-the-lit'
  if (!samePlace(now.rect, lit.rect) || at - lit.movedAt < DOM_AIM.calmMs) return 'moved'
  if (cameAt !== null && at - cameAt < DOM_AIM.calmMs) return 'came'
  if (at - lit.since < DOM_AIM.stillMs) return 'too-soon'
  return null
}
