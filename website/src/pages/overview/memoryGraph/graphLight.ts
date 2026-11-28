/**
 * Lighting the memories a search or a recall found: each hit glows by how well
 * it matched, every other mark dims and turns grey, and the change fades in and
 * out. Pure helpers; MemoryGraphTab owns the canvas and the fade's clock.
 */
import type { Rgb } from './graphColors'
import { LINK_DIM_SHARE, LINK_LIT_ALPHA } from './graphView'

/** One memory the server lit: its cluster and its score (1 for a word match). */
export type LitMatch = { id: string; cluster: number; score: number }

/** A lit set: each hit's glow (0 weakest to 1 best of this set) and the hits per cluster. */
export type Lit = { glow: Map<string, number>; clusters: Map<number, number> }

/** A dimmed mark's opacity, and how much of its colour turns grey. */
export const DIM_ALPHA = 0.1
export const DIM_GREY = 0.8
/** The weakest hit's opacity; the best is fully opaque. */
export const HIT_MIN_ALPHA = 0.7
/** How many times its own size a hit is drawn: the weakest, and the best. A hit
 *  must stand out at the overview, where a mark is 3 px. */
export const HIT_MIN_SCALE = 1.5
export const HIT_MAX_SCALE = 2.2

/** A drawn mark: colour, opacity and size in CSS px. */
export type Mark = { rgb: Rgb; alpha: number; px: number }

/**
 * The lit set for the server's matches. Glow is relative to the set's own range,
 * so the best match of any search is the brightest and the weakest still shows.
 */
export function litFrom(matches: LitMatch[]): Lit {
  let lo = Infinity, hi = -Infinity
  for (const m of matches) { lo = Math.min(lo, m.score); hi = Math.max(hi, m.score) }
  const span = hi - lo
  const glow = new Map<string, number>(), clusters = new Map<number, number>()
  for (const m of matches) {
    glow.set(m.id, span > 1e-6 ? (m.score - lo) / span : 1)
    clusters.set(m.cluster, (clusters.get(m.cluster) ?? 0) + 1)
  }
  return { glow, clusters }
}

/** *rgb* moved *share* of the way to its own grey. */
export function toGrey([r, g, b]: Rgb, share: number): Rgb {
  const y = 0.2126 * r + 0.7152 * g + 0.0722 * b
  return [r + (y - r) * share, g + (y - g) * share, b + (y - b) * share]
}

/** How many times its own size a lit set draws a mark: a hit by its glow, anything else as it is. */
export function litScale(lit: Lit | null, glow: number | undefined): number {
  return lit && glow !== undefined ? HIT_MIN_SCALE + (HIT_MAX_SCALE - HIT_MIN_SCALE) * glow : 1
}

/** *base* as a lit set shows it: a hit by its glow, anything else dimmed. No set leaves it. */
export function litMark(base: Mark, lit: Lit | null, glow: number | undefined): Mark {
  if (!lit) return base
  if (glow === undefined) return { rgb: toGrey(base.rgb, DIM_GREY), alpha: DIM_ALPHA, px: base.px }
  return {
    rgb: base.rgb,
    alpha: Math.max(base.alpha, HIT_MIN_ALPHA + (1 - HIT_MIN_ALPHA) * glow),
    px: base.px * litScale(lit, glow),
  }
}

export const lerp = (a: number, b: number, t: number) => a + (b - a) * t

/** The mark *t* of the way from *a* to *b*. */
export function mixMarks(a: Mark, b: Mark, t: number): Mark {
  if (t >= 1) return b
  if (t <= 0) return a
  return { rgb: [lerp(a.rgb[0], b.rgb[0], t), lerp(a.rgb[1], b.rgb[1], t), lerp(a.rgb[2], b.rgb[2], t)], alpha: lerp(a.alpha, b.alpha, t), px: lerp(a.px, b.px, t) }
}

/** How much stronger a link between two hits is drawn than unlit. */
export const LIT_LINK_GAIN = 3

/** A link's opacity under a lit set, from its unlit *alpha*: stronger when it
 *  joins two hits, so related hits read as one firing, and dimmed otherwise. */
export function litLinkAlpha(alpha: number, lit: Lit | null, a: string, b: string): number {
  if (!lit) return alpha
  return lit.glow.has(a) && lit.glow.has(b) ? Math.min(LINK_LIT_ALPHA, alpha * LIT_LINK_GAIN) : alpha * LINK_DIM_SHARE
}

/** A topic's name while a set is lit: its hits counted after it. */
export const litTopic = (label: string, hits: string) => `${label} · ${hits}`

/** A fade between two lit sets (or none): where it started, where it goes, how far it is. */
export type Fade = { from: Lit | null; to: Lit | null; t: number }

/** The ease every fade uses: the dashboard's ease-out, as the camera moves. */
export const easeOut = (t: number) => 1 - (1 - t) ** 3
