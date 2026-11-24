/**
 * Two in view (D66): two pages side by side in the lens with the head at rest. A state he asks for with the
 * palette's tile (`PaletteTwoTile.tsx`), in a session; the same tile ends it and both pages go back where and as
 * big as they were. Opening a second page stays as it was (`layout.ts`, `placeNew`).
 *
 * The pair is 800 px a page, a little farther than a page opens at, so the dashboard keeps its desktop layout.
 * With `two=a` in the address it is 600 px a page at the distance a page opens at: the text keeps its size and the
 * chat page folds to its phone layout (kept behind the word until the glasses say which reads better).
 *
 * A page and a narrow strip beside it (`TWO_STRIP`) waits: its swap belongs on the strip itself.
 */
import { M_PER_PX, PAGES, type Slot } from './layout'

export type TwoWay = 'a' | 'd'

/** The way the pair stands: the address's word, or the ruled one. */
export const twoWay = (search: string): TwoWay => (new URLSearchParams(search).get('two') === 'a' ? 'a' : 'd')

/**
 * How much of the view the Aura's lens holds from side to side, how much of it two pages take with their gap, and
 * how far under level the pair stands: Home's strip then has room over the pair's close marks, inside the lens.
 */
export const TWO = { lensDeg: 61, pairDeg: 60.4, gapDeg: 1, pitchDeg: -2 } as const

/** The page and the strip of the way that waits: a page at its full width, a strip as narrow as a page can be. */
export const TWO_STRIP = { pageCss: PAGES.cssWidth, stripCss: PAGES.minCssWidth } as const

/** One page of a pair: how wide and high its page is laid out, and where it stands. */
export interface TwoPage { css: [number, number]; at: Slot }

/** What a page of a pair goes back to when the pair ends: its place, its size, and whether a hand had placed it. */
export interface TwoBefore { at: Slot; css: [number, number]; byHand: boolean }

const DEG = 180 / Math.PI
/** How wide a page of this many CSS pixels is at this distance, as an angle at the eye. */
export const widthDeg = (cssWidth: number, distanceM: number): number => 2 * Math.atan((cssWidth * M_PER_PX) / 2 / distanceM) * DEG
/** How far away a page of this many CSS pixels is this wide. */
const distanceFor = (cssWidth: number, deg: number): number => (cssWidth * M_PER_PX) / 2 / Math.tan(deg / 2 / DEG)

/** The two pages of a pair round where he looks (`lookYawDeg`, Y2): the left one, the right one, the gap between. */
export function twoPair(way: TwoWay, lookYawDeg = 0): [TwoPage, TwoPage] {
  const css = way === 'a' ? 600 : 800
  const each = (TWO.pairDeg - TWO.gapDeg) / 2
  const distanceM = way === 'a' ? PAGES.distanceM : distanceFor(css, each)
  const wide = widthDeg(css, distanceM)
  const at = (yawDeg: number): Slot => ({ yawDeg: lookYawDeg + yawDeg, pitchDeg: TWO.pitchDeg, distanceM })
  return [{ css: [css, PAGES.cssHeight], at: at(-(wide + TWO.gapDeg) / 2) }, { css: [css, PAGES.cssHeight], at: at((wide + TWO.gapDeg) / 2) }]
}

/**
 * Which two of the open pages form the pair: the page ahead of him (nearest to where he looks) and the page he
 * opened last (the last of the list that is not that one). `null` with fewer than two.
 */
export function twoOf(pages: readonly { id: string; yawDeg: number }[], lookYawDeg: number): [string, string] | null {
  if (pages.length < 2) return null
  const off = (yawDeg: number) => Math.abs(((yawDeg - lookYawDeg + 540) % 360) - 180)
  const ahead = pages.reduce((a, b) => (off(b.yawDeg) < off(a.yawDeg) ? b : a))
  const last = [...pages].reverse().find(p => p.id !== ahead.id)
  if (!last) return null
  // The one that stands more to the left goes left: neither page crosses the other on its way.
  return ahead.yawDeg <= last.yawDeg ? [ahead.id, last.id] : [last.id, ahead.id]
}

const num = (v: unknown, min: number, max: number): number | null => (typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max ? v : null)

/** What the room's record kept of a pair's page (`room.ts`), read with care; `undefined` when it is not whole. */
export function twoBeforeOf(raw: unknown): TwoBefore | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const r = raw as Record<string, unknown>
  const at = (r.at && typeof r.at === 'object' ? r.at : {}) as Record<string, unknown>
  const css = Array.isArray(r.css) ? r.css : []
  const yawDeg = num(at.yawDeg, -180, 180), pitchDeg = num(at.pitchDeg, -89, 89), distanceM = num(at.distanceM, PAGES.nearM, PAGES.farM)
  const w = num(css[0], PAGES.minCssWidth, PAGES.maxCssWidth), h = num(css[1], PAGES.minCssHeight, PAGES.maxCssHeight)
  if (yawDeg === null || pitchDeg === null || distanceM === null || w === null || h === null) return undefined
  return { at: { yawDeg, pitchDeg, distanceM }, css: [Math.round(w), Math.round(h)], byHand: r.byHand === true }
}
