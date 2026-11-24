/**
 * Which of two hits the head's dot takes (D78 1): a page's frame (its move band, a corner, its close mark: what a
 * panel marks `xrFrame`) is taken before a Bot or the board at its desk that stands nearer on the same ray, so that
 * a page's edge is never lost behind the room's Bots. Every other pair goes by the one order of `drawOrder.ts` (M37),
 * which each layer's root carries as its `pointerEventsOrder`, then the nearer: Home's strip and names before any
 * part of a page, a page carried nearer than Home included; a held board and the palette before Home. So a Bot in
 * front of a page's content keeps its own pinch (pages and Bots share the room's order).
 */
import type { Object3D } from 'three'

export type HitKind = 'frame' | 'bot' | 'home' | 'page' | 'other'

interface Hit { object: Object3D; distance: number }

/** What a hit is for this rule: walked up from the object that was hit. */
export function hitKind(object: Object3D): HitKind {
  for (let o: Object3D | null = object; o; o = o.parent) {
    if (typeof o.userData.xrFrame === 'string') return 'frame'
    if (o.userData.xrPress === 'bot') return 'bot'
    // Any part of a board (its tag, its clip, its face, its stamp): the room's, as a Bot is, unless it is held. Its
    // group says which (`Board.tsx`, M39), wherever it stands: the second held board stands 0.80 m off.
    if (o.userData.xrPress === 'board') return o.userData.xrHeld === true ? 'other' : 'bot'
    if (o.userData.xrPress === 'home') return 'home'
    if (typeof o.userData.xrPanel === 'string' || typeof o.userData.xrPage === 'string') return 'page'
  }
  return 'other'
}


/** The Bot a hit is on (its slug), or null: the plate a pointer meets carries it (`Crew.tsx`). */
export function botOf(object: Object3D): string | null {
  for (let o: Object3D | null = object; o; o = o.parent) if (typeof o.userData.xrBot === 'string') return o.userData.xrBot as string
  return null
}

/** `customSort` for the dot's ray: negative when `a` is taken before `b`. */
export function frameFirst(a: Hit, orderA: number | undefined, b: Hit, orderB: number | undefined): number {
  const ka = hitKind(a.object)
  const kb = hitKind(b.object)
  if (ka === 'frame' && kb === 'bot') return -1
  if (kb === 'frame' && ka === 'bot') return 1
  // Every other pair by the one order (`drawOrder.ts`, M37): Home before any part of a page, a page carried in front
  // of it included; a held board before Home; then the nearer.
  const oa = orderA ?? 0
  const ob = orderB ?? 0
  if (oa !== ob) return ob - oa
  return a.distance - b.distance
}
