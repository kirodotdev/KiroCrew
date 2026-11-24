/**
 * The land: where each Bot's plot is, and how the whole land is shown to the
 * person, from a miniature on the desk to full size around them.
 *
 * Everything here is plain numbers, so the rules can be tested without a
 * scene. Land coordinates are metres at full size: x to the right, z toward
 * the person, the ground at y = 0. The person looks toward -z.
 *
 * Plots. A Bot's plot is a cell of a square spiral. A Bot keeps the cell it
 * was first given: the map from Bot to cell only ever gains entries, so a
 * plot never moves when Bots are added, removed or listed in another order.
 *
 * The view is one number and a point. `t` runs from 0 (the whole land as a
 * miniature on the desk) to 1 (full size, the person standing in a plot), and
 * the focus is the land point held at the anchor. The scale is a power of
 * `t`, so equal hand movements feel like equal zoom steps, and the anchor
 * slides from the desk top to the floor under the person with an eased curve.
 * There is no rotation anywhere: the horizon stays level, and nothing cuts.
 */
import { TUNE, type TwoHandRule } from './tune'


/** The side of a plot, and how tall a Bot is in it, in metres at full size. */
export const PLOT_M = 6
export const BOT_M = 1.1
/** A Bot is never drawn smaller than this, so it can be found and pointed at in the miniature. */
export const BOT_MIN_M = 0.036
/** Where the miniature sits, relative to the head: below it, ahead of it, and how wide it is. */
export const DESK = { y: -0.42, z: -0.6, across: 0.72 }
/** A zoom let go this close to an end goes on to that end. */
const SETTLE = 0.12

export type PlotMap = Readonly<Record<string, number>>
export interface LandView { t: number; fx: number; fz: number }
export interface LandBounds { cx: number; cz: number; radius: number }
export interface LandPose { s: number; x: number; y: number; z: number }

const clamp01 = (v: number): number => Math.max(0, Math.min(1, v))
const smooth = (t: number): number => t * t * (3 - 2 * t)

/** Cell i of a square spiral that starts at the middle and winds outward. */
export function spiralCell(i: number): [number, number] {
  let x = 0
  let z = 0
  let dx = 1
  let dz = 0
  let run = 1
  let done = 0
  let turns = 0
  for (let k = 0; k < i; k += 1) {
    x += dx
    z += dz
    done += 1
    if (done < run) continue
    done = 0
    ;[dx, dz] = [-dz, dx]
    turns += 1
    if (turns % 2 === 0) run += 1
  }
  return [x, z]
}

/** Give every Bot a cell. A Bot that has one keeps it; a new Bot takes the first free cell. */
export function assignPlots(prev: PlotMap, slugs: readonly string[]): PlotMap {
  const fresh = slugs.filter(s => !Object.hasOwn(prev, s))
  if (!fresh.length) return prev
  const out: Record<string, number> = { ...prev }
  const taken = new Set(Object.values(out))
  let next = 0
  for (const s of fresh) {
    while (taken.has(next)) next += 1
    out[s] = next
    taken.add(next)
  }
  return out
}

/** Read a stored plot map. Anything that is not a map of distinct whole cells is dropped whole. */
export function parsePlots(raw: string | null | undefined): PlotMap {
  if (!raw) return {}
  try {
    const v: unknown = JSON.parse(raw)
    if (!v || typeof v !== 'object' || Array.isArray(v)) return {}
    const cells = Object.values(v)
    const whole = cells.every(c => typeof c === 'number' && Number.isInteger(c) && c >= 0 && c < 10_000)
    return whole && new Set(cells).size === cells.length ? (v as PlotMap) : {}
  } catch {
    return {}
  }
}

/** The middle of a plot, in land metres. */
export function plotCentre(cell: number): [number, number] {
  const [x, z] = spiralCell(cell)
  return [x * PLOT_M, z * PLOT_M]
}

/** Where the person stands when they are in a plot: at its near edge, facing the Bot's home in the middle. */
export function visitorSpot(cell: number): [number, number] {
  const [x, z] = plotCentre(cell)
  return [x, z + PLOT_M * 0.32]
}

/** The middle and the reach of the plots these Bots have. */
export function landBounds(map: PlotMap, slugs: readonly string[]): LandBounds {
  const pts = slugs.filter(s => Object.hasOwn(map, s)).map(s => plotCentre(map[s]))
  if (!pts.length) return { cx: 0, cz: 0, radius: PLOT_M }
  const xs = pts.map(p => p[0])
  const zs = pts.map(p => p[1])
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2
  const cz = (Math.min(...zs) + Math.max(...zs)) / 2
  const radius = Math.max(...pts.map(p => Math.hypot(p[0] - cx, p[1] - cz))) + PLOT_M * 0.75
  return { cx, cz, radius }
}

/** The Bot whose plot holds a land point, or the nearest plot's Bot. */
export function plotAt(map: PlotMap, slugs: readonly string[], x: number, z: number): string {
  let best = ''
  let bestD = Infinity
  for (const s of slugs) {
    if (!Object.hasOwn(map, s)) continue
    const [px, pz] = plotCentre(map[s])
    const d = Math.hypot(px - x, pz - z)
    if (d < bestD) { best = s; bestD = d }
  }
  return best
}

/** The scale at which the whole land fits on the desk. */
export const minScale = (radius: number): number => Math.min(1, DESK.across / (2 * radius))

/** The land's scale at a zoom step: a power of `t`, from the miniature's scale to 1. */
export const scaleAt = (t: number, sMin: number): number => Math.pow(sMin, 1 - clamp01(t))

/** Where the land's origin is and how big the land is, in the person's own space (the head start is the origin). */
export function landPose(v: LandView, sMin: number, eyeY: number): LandPose {
  const s = scaleAt(v.t, sMin)
  const e = smooth(clamp01(v.t))
  // The focus is held at an anchor that slides from the desk top to the floor under the person.
  const ay = DESK.y + (-eyeY - DESK.y) * e
  const az = DESK.z * (1 - e)
  return { s, x: -v.fx * s, y: ay, z: az - v.fz * s }
}

/**
 * Two hands pulled apart or pushed together. `ratio` is how far apart they
 * are now against a moment ago. Zooming in also draws the focus toward
 * `toward` (the plot looked at), in step with the zoom, so it arrives exactly
 * when the land reaches full size. Zooming out leaves the focus alone.
 */
export function zoomBy(v: LandView, ratio: number, sMin: number, toward: readonly [number, number] | null): LandView {
  if (!(ratio > 0) || !(sMin < 1)) return v
  const t = clamp01(v.t + Math.log(ratio) / Math.log(1 / sMin))
  if (t <= v.t || !toward) return { ...v, t }
  const k = (t - v.t) / (1 - v.t)
  return { t, fx: v.fx + (toward[0] - v.fx) * k, fz: v.fz + (toward[1] - v.fz) * k }
}

/**
 * What a change of the gap between two real hands asks of the land, as a zoom ratio.
 *
 * With the `window` rule the pull is measured in metres, not as a ratio of the gap the hands happened to start with:
 * `gapOpenM - gapCloseM` of pull is the whole way from the miniature to full size, wherever the hands took hold.
 * With the `double` rule the land's scale goes with the ratio of the gap, to the power `zoomGain`.
 */
export function handZoom(fromGapM: number, toGapM: number, sMin: number, rule: TwoHandRule = TUNE.twoHandRule): number {
  if (!(sMin < 1) || !Number.isFinite(toGapM - fromGapM)) return 1
  if (rule === 'double') return fromGapM > 0 && toGapM > 0 ? Math.pow(toGapM / fromGapM, TUNE.zoomGain) : 1
  return Math.pow(1 / sMin, (toGapM - fromGapM) / (TUNE.gapOpenM - TUNE.gapCloseM))
}

/**
 * Whether a zoom that was just let go carries on by itself: 1 to full size, -1 to the miniature, 0 to stay.
 * `samples` are the zoom (0..1) at times in ms. It is judged on the fastest moment of the last `zoomFlingMs`,
 * so hands that stopped before they let go leave the land where it is.
 */
export function zoomFling(samples: readonly { at: number; t: number }[]): -1 | 0 | 1 {
  const last = samples.at(-1)
  if (!last) return 0
  let best = 0
  for (let i = 1; i < samples.length; i += 1) {
    const a = samples[i - 1]
    const b = samples[i]
    if (last.at - a.at > TUNE.zoomFlingMs || b.at <= a.at) continue
    const rate = ((b.t - a.t) * 1000) / (b.at - a.at)
    if (Math.abs(rate) > Math.abs(best)) best = rate
  }
  return Math.abs(best) >= TUNE.zoomFlingPerS ? (best > 0 ? 1 : -1) : 0
}

/** One hand drags the ground: `dx`, `dz` are metres in the person's space. The focus stays on the land. */
export function panBy(v: LandView, dx: number, dz: number, sMin: number, b: LandBounds): LandView {
  const s = scaleAt(v.t, sMin)
  let fx = v.fx - dx / s
  let fz = v.fz - dz / s
  const d = Math.hypot(fx - b.cx, fz - b.cz)
  if (d > b.radius) { fx = b.cx + ((fx - b.cx) * b.radius) / d; fz = b.cz + ((fz - b.cz) * b.radius) / d }
  return { t: v.t, fx, fz }
}

/** Where a zoom that was let go comes to rest: at an end when it is close to one, else where it is. */
export const settleT = (t: number): number => (t < SETTLE ? 0 : t > 1 - SETTLE ? 1 : t)

/** Move the shown view toward the wanted one. It starts fast and slows into place; `still` jumps there. */
export function follow(cur: LandView, goal: LandView, dt: number, still = false): LandView {
  const k = still ? 1 : 1 - Math.exp(-dt * 7)
  const step = (a: number, b: number) => (Math.abs(b - a) < 1e-5 ? b : a + (b - a) * k)
  return { t: step(cur.t, goal.t), fx: step(cur.fx, goal.fx), fz: step(cur.fz, goal.fz) }
}

/** How tall a Bot is drawn, in the person's metres, at a land scale. */
export const botHeight = (s: number): number => Math.max(BOT_M * s, BOT_MIN_M)
