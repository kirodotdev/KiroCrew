/**
 * The page he looks at, on the stream (P1 speed, step B; D41). The gateway paces a streamed page that no pointer is
 * on at ten pictures a second or less; the page he looks at is told to it (the socket word `watch`) and gets the
 * moving pace, though no pointer is on it. One page at a time.
 *
 * The pick: the page in view nearest the middle of his look, within `WATCH.withinDeg` of it: his look meets the page,
 * or misses its edge by a hair (the measure is to the page's nearest point, `offLookDeg`). A page becomes the
 * watched one when it has been the nearest for `WATCH.restMs`; it stays watched until another page has been the
 * nearest for as long, it leaves his view, or no page has been near his look for `WATCH.endMs` (he looks at the
 * Bots: a chat at the side of his view does not stay fast). No page is watched while his hands are busy with a board or a speak
 * sheet, or while the pages are put away.
 */
import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'

/**
 * How near the middle of his look a page's nearest point must be to count (degrees: 0 is a look that meets the
 * page, so 5 is a look that just misses its edge; a larger number leaves no level look between three pages of the
 * ring that is "at no page"); how long a look must rest on a page before it is watched (ms); and how long no page
 * may be near his look before the watch ends (ms).
 */
export const WATCH = { withinDeg: 5, restMs: 300, endMs: 1500 } as const

interface Xyz { x: number; y: number; z: number }

/**
 * How far a page is from the middle of his look, in degrees: the angle from his look to the point of the page that
 * is nearest to it. In the page's own frame: the page is the rectangle of middle (`cx`, `cy`) and size `w` by `h` in
 * the plane z = 0; `eye` is where he looks from and `ahead` the way he looks. 0 when his look meets the page. A look
 * that never meets the page's plane (level with it, or away from it) is measured to the page's middle. `far` is how
 * far that point is from him (the frame's own units): of two pages his look meets, the one in front is the nearer.
 */
export function offLook(eye: Xyz, ahead: Xyz, page: { cx: number; cy: number; w: number; h: number }): { deg: number; far: number } {
  const t = Math.abs(ahead.z) > 1e-9 ? -eye.z / ahead.z : -1
  const within = (v: number, mid: number, size: number) => Math.max(mid - size / 2, Math.min(mid + size / 2, v))
  const hx = eye.x + ahead.x * t
  const hy = eye.y + ahead.y * t
  const px = t > 0 ? within(hx, page.cx, page.w) : page.cx
  const py = t > 0 ? within(hy, page.cy, page.h) : page.cy
  const to = { x: px - eye.x, y: py - eye.y, z: -eye.z }
  const far = Math.hypot(to.x, to.y, to.z)
  // His look meets the page: exactly 0, so that two pages it meets tie (`pickWatched`). The cosine of a look that
  // meets the page is 1 only up to rounding, and its arc cosine is then a hair over 0.
  if (t > 0 && px === hx && py === hy) return { deg: 0, far }
  const dot = ahead.x * to.x + ahead.y * to.y + ahead.z * to.z
  const len = Math.hypot(ahead.x, ahead.y, ahead.z) * far
  return { deg: len > 0 ? (Math.acos(Math.max(-1, Math.min(1, dot / len))) * 180) / Math.PI : 0, far }
}

/** `offLook`'s angle alone. */
export const offLookDeg = (eye: Xyz, ahead: Xyz, page: { cx: number; cy: number; w: number; h: number }): number => offLook(eye, ahead, page).deg

/** A streamed page as the pick sees it. `far`: how far from him its point nearest his look is (a tie's second word). */
export interface WatchPage { id: string; inView: boolean; gazeDeg: number; far?: number }
/** What the pick keeps between frames. */
export interface WatchState {
  watched: string | null
  /** The page nearest his look that is not the watched one, and since when it is the nearest. */
  nearest: string | null
  since: number
  /** Since when no page is near his look; null while one is. */
  none: number | null
}

export const noWatch = (): WatchState => ({ watched: null, nearest: null, since: 0, none: null })

/** The next state: which page is watched now. `blocked`: no page is watched (a board in his hand, pages put away). */
export function pickWatched(pages: readonly WatchPage[], was: WatchState, now: number, blocked: boolean): WatchState {
  if (blocked) return noWatch()
  // Of two pages equally near (his look meets both, one before the other): the watched one stays; with neither
  // watched, the one in front.
  const beats = (p: WatchPage, q: WatchPage) =>
    p.gazeDeg !== q.gazeDeg ? p.gazeDeg < q.gazeDeg : p.id === was.watched || (q.id !== was.watched && (p.far ?? Infinity) < (q.far ?? Infinity))
  let near: WatchPage | null = null
  for (const p of pages) if (p.inView && p.gazeDeg <= WATCH.withinDeg && (!near || beats(p, near))) near = p
  const nearest = near?.id ?? null
  // The watched page left his view (or closed): it is watched no more.
  const watched = was.watched && pages.some(p => p.id === was.watched && p.inView) ? was.watched : null
  if (nearest === null) {
    // He looks at no page: the watch ends once that has lasted.
    const none = was.none ?? now
    return { watched: now - none >= WATCH.endMs ? null : watched, nearest: null, since: now, none }
  }
  if (nearest === watched) return { watched, nearest: null, since: now, none: null }
  if (nearest !== was.nearest) return { watched, nearest, since: now, none: null }
  return now - was.since >= WATCH.restMs ? { watched: nearest, nearest: null, since: now, none: null } : { watched, nearest, since: was.since, none: null }
}

/** A page that can be watched: a streamed page says where it is in his look, and is told whether it is watched. */
export interface Watchable { readonly id: string; watchPage(): WatchPage; watch(on: boolean): void }

/** The streamed pages there are now (each adds itself, and takes itself out when it goes). */
export const watchables = new Set<Watchable>()

/**
 * Picks the watched page once a room frame and tells each page. Mounted inside the canvas, after the panels, so
 * that it reads this frame's look.
 */
export function StreamWatch({ blocked }: { blocked: boolean }): null {
  const state = useRef(noWatch())
  useFrame(() => {
    const was = state.current
    const next = pickWatched([...watchables].map(w => w.watchPage()), was, performance.now(), blocked)
    state.current = next
    if (next.watched !== was.watched) for (const w of watchables) w.watch(w.id === next.watched)
  })
  return null
}
