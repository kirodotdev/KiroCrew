/**
 * The memory graph's view, in the layout's own units (the server's x and y).
 *
 * The camera is a centre and a scale: `k` screen pixels per layout unit. Every
 * conversion, the level of detail and the label placement are pure functions of
 * it, so the canvas (cosmos.gl) only draws what these decide.
 */
import type { GraphBbox, GraphCluster } from '../../../api/client/memory'

export type Point = { x: number; y: number }
export type View = { cx: number; cy: number; k: number }
export type Size = { width: number; height: number }

export const MIN_K = 1e-4
export const MAX_K = 400
/** Most nodes the canvas holds and draws; past it the view shows clusters. A
 *  store up to this size is drawn as memories at every zoom, as the earlier
 *  graph was: cosmos.gl draws 100k points in under a millisecond a frame. */
export const NODE_BUDGET = 50000
/** Extra margin fetched around the view, as a share of its size, so a short pan needs no request. */
export const FETCH_MARGIN = 0.5

const clampK = (k: number) => Math.min(MAX_K, Math.max(MIN_K, k))

/** Screen position (y down) of a layout point. */
export const toScreen = (view: View, size: Size, p: Point): Point => ({
  x: size.width / 2 + (p.x - view.cx) * view.k,
  y: size.height / 2 - (p.y - view.cy) * view.k,
})

/** Layout point under a screen position. */
export const toLayout = (view: View, size: Size, s: Point): Point => ({
  x: view.cx + (s.x - size.width / 2) / view.k,
  y: view.cy - (s.y - size.height / 2) / view.k,
})

/** The layout box the screen shows, grown by `margin` of its size on each side. */
export function visibleBox(view: View, size: Size, margin = 0): GraphBbox {
  const w = size.width / view.k, h = size.height / view.k
  return [view.cx - w * (0.5 + margin), view.cy - h * (0.5 + margin), view.cx + w * (0.5 + margin), view.cy + h * (0.5 + margin)]
}

/** The layout box of a screen rectangle (a box selection). */
export function screenRectToBox(view: View, size: Size, r: { x: number; y: number; width: number; height: number }): GraphBbox {
  const a = toLayout(view, size, { x: r.x, y: r.y }), b = toLayout(view, size, { x: r.x + r.width, y: r.y + r.height })
  return [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)]
}

/** The view that shows `box` with `pad` screen pixels to spare. */
export function fitBox(box: GraphBbox, size: Size, pad = 32): View {
  const w = Math.max(box[2] - box[0], 1e-6), h = Math.max(box[3] - box[1], 1e-6)
  const k = clampK(Math.min((size.width - 2 * pad) / w, (size.height - 2 * pad) / h))
  return { cx: (box[0] + box[2]) / 2, cy: (box[1] + box[3]) / 2, k }
}

/** Slide the view by a screen delta. */
export const panBy = (view: View, d: Point): View => ({ ...view, cx: view.cx + d.x / view.k, cy: view.cy - d.y / view.k })

/** Zoom by `factor` (above 1 zooms out) keeping the layout point under `at` still. */
export function zoomAt(view: View, size: Size, at: Point, factor: number): View {
  const k = clampK(view.k / factor)
  const fixed = toLayout(view, size, at)
  return { k, cx: fixed.x - (at.x - size.width / 2) / k, cy: fixed.y + (at.y - size.height / 2) / k }
}

/** The share of cluster `c`'s square (centre, half-width `r`) that lies inside `a`. */
function shareInside(a: GraphBbox, c: GraphCluster): number {
  const w = Math.min(a[2], c.x + c.r) - Math.max(a[0], c.x - c.r)
  const h = Math.min(a[3], c.y + c.r) - Math.max(a[1], c.y - c.r)
  if (w < 0 || h < 0) return 0
  return c.r > 0 ? Math.min(1, (w * h) / (4 * c.r * c.r)) : 1
}

/**
 * Whether the view shows nodes or clusters: nodes once the view holds about
 * `NODE_BUDGET` rows or fewer, counting each cluster by the share of it in view.
 * A small store shows every node at once; a large one, or one large cluster,
 * opens into nodes as you zoom in.
 */
export function showsNodes(view: View, size: Size, clusters: GraphCluster[]): boolean {
  const box = visibleBox(view, size)
  let rows = 0
  for (const c of clusters) { rows += c.count * shareInside(box, c); if (rows > NODE_BUDGET) return false }
  return true
}

/** True when `inner` lies wholly inside `outer`. */
export const contains = (outer: GraphBbox, inner: GraphBbox) => inner[0] >= outer[0] && inner[1] >= outer[1] && inner[2] <= outer[2] && inner[3] <= outer[3]

/**
 * A label to place. `px` is its text size (12 when unset); `color` its text
 * colour (the page's text colour when unset); `centred` puts it over its point,
 * as a topic's name sits over its cluster, instead of just above it; `mark`
 * (the mark's size in px) puts it beside its memory's mark instead.
 */
export type LabelCandidate = { id: string; at: Point; text: string; rank: number; px?: number; color?: string; centred?: boolean; mark?: number }
export type PlacedLabel = LabelCandidate & { box: { x: number; y: number; width: number; height: number } }

/** Most characters of a memory's name on the canvas; hover shows the whole label. */
export const NODE_LABEL_CHARS = 32

/** *text* on one line, at most *max* characters: cut at the last word that fits, then "…".
 *  Text with no space to cut at (a long token, CJK) is cut at the limit. */
export function cutAtWord(text: string, max = NODE_LABEL_CHARS): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  const chars = Array.from(flat)
  if (chars.length <= max) return flat
  const head = chars.slice(0, max - 1).join('')
  const space = head.lastIndexOf(' ')
  const cut = space >= head.length / 2 ? head.slice(0, space) : head
  return `${cut.replace(/[\s.,;:·\-–—]+$/u, '')}…`
}

/**
 * A memory's short name on the canvas. A fact is named by its key's last part
 * (`pref.kirocrew.cli_help_probe` reads "cli help probe"), not its whole
 * `key = value`; a lesson or an experience by its own text. Either is cut at a word.
 */
export function canvasLabel(node: { kind: string; label: string; record?: { id: string } }): string {
  if (node.kind === 'fact' && node.record?.id) {
    const last = node.record.id.split('.').filter(Boolean).pop() ?? node.record.id
    const words = last.replace(/[_-]+/g, ' ').trim()
    if (words) return cutAtWord(words)
  }
  return cutAtWord(node.label)
}

/** Clear space kept around every label, so no two ever touch. */
export const LABEL_GAP = 8
export const LABEL_PX = 12

/** A label's horizontal padding (the overlay's `px-1.5`), inside its box. */
export const LABEL_PAD = 6
/** Clear space between a mark and the label beside it. */
export const MARK_GAP = 6
/** A label beside a mark may still cover this many other marks; more and it is not drawn. */
export const MAX_COVERED_MARKS = 2

/** A mark drawn on screen: its centre and its size in CSS pixels. */
export type ScreenMark = { id: string; at: Point; px: number }

type LabelBox = PlacedLabel['box']

/** How many marks other than *own* reach into *box*. */
function marksUnder(box: LabelBox, marks: ScreenMark[], own: string): number {
  let n = 0
  for (const m of marks) {
    if (m.id === own) continue
    const r = m.px / 2
    if (m.at.x + r > box.x && m.at.x - r < box.x + box.width && m.at.y + r > box.y && m.at.y - r < box.y + box.height) n++
  }
  return n
}

/**
 * Up to `max` labels, highest `rank` first, none within `LABEL_GAP` pixels of
 * another, none off screen, and no text twice. `widthOf` measures a label in
 * pixels at its size; the height is one line of 1.5 times that size.
 *
 * A candidate with `mark` (its mark's size in px) sits beside that mark,
 * vertically centred, `MARK_GAP` px from its edge: on the right, or on the
 * left when that side covers fewer of the other `marks`; a label that would
 * still cover more than `MAX_COVERED_MARKS` of them is left out.
 */
export function placeLabels(cands: LabelCandidate[], size: Size, max: number, widthOf: (text: string, px: number) => number, marks: ScreenMark[] = []): PlacedLabel[] {
  const out: PlacedLabel[] = []
  const seen = new Set<string>()
  const sorted = [...cands].sort((a, b) => b.rank - a.rank || (a.id < b.id ? -1 : 1))
  const g = LABEL_GAP
  const fits = (box: LabelBox) => box.x >= 0 && box.y >= 0 && box.x + box.width <= size.width && box.y + box.height <= size.height
    && !out.some(o => box.x < o.box.x + o.box.width + g && o.box.x < box.x + box.width + g && box.y < o.box.y + o.box.height + g && o.box.y < box.y + box.height + g)
  for (const c of sorted) {
    if (out.length >= max) break
    if (seen.has(c.text)) continue
    const px = c.px ?? LABEL_PX
    const width = widthOf(c.text, px), height = Math.round(px * 1.5)
    let box: LabelBox | undefined
    if (c.mark !== undefined) {
      const y = c.at.y - height / 2, reach = c.mark / 2 + MARK_GAP - LABEL_PAD
      const sides = [{ x: c.at.x + reach, y, width, height }, { x: c.at.x - reach - width, y, width, height }]
        .filter(fits).map(b => ({ b, covered: marksUnder(b, marks, c.id) }))
        .sort((p, q) => p.covered - q.covered)
      if (sides.length && sides[0].covered <= MAX_COVERED_MARKS) box = sides[0].b
    } else {
      const b = { x: c.at.x - width / 2, y: c.centred ? c.at.y - height / 2 : c.at.y - height - 6, width, height }
      if (fits(b)) box = b
    }
    if (!box) continue
    seen.add(c.text)
    out.push({ ...c, box })
  }
  return out
}

/** Links between related memories: the cosine the server keeps them at (recall's
 *  floor), and how they fade in. With the whole store in view a link is barely
 *  there, so the overview stays calm; it reaches full strength by 16x zoom. */
export const LINK_FLOOR = 0.55
export const LINK_OVERVIEW_ALPHA = 0.05
export const LINK_MAX_ALPHA = 0.35
export const LINK_FULL_ZOOM = 16
/** A hovered or focused memory's own links, and how much the rest dim meanwhile. */
export const LINK_LIT_ALPHA = 0.9
export const LINK_DIM_SHARE = 0.3

/**
 * A link's opacity at `zoom` (1 = the whole store in view) for its cosine
 * `sim`: rising from LINK_OVERVIEW_ALPHA to LINK_MAX_ALPHA as the view zooms
 * in, and stronger for a closer pair. `lit` is the hovered memory's own link;
 * `dim` is any other link while one is hovered.
 */
export function linkAlpha(zoom: number, sim: number, focus: 'lit' | 'dim' | 'none' = 'none'): number {
  if (focus === 'lit') return LINK_LIT_ALPHA
  const lod = Math.min(1, Math.log2(Math.max(1, zoom)) / Math.log2(LINK_FULL_ZOOM))
  const close = 0.4 + 0.6 * Math.min(1, Math.max(0, (sim - LINK_FLOOR) / (1 - LINK_FLOOR)))
  const a = (LINK_OVERVIEW_ALPHA + (LINK_MAX_ALPHA - LINK_OVERVIEW_ALPHA) * lod) * close
  return focus === 'dim' ? a * LINK_DIM_SHARE : a
}

/** A drawn link: point indices `a < b`, its cosine, and the cluster its colour comes from. */
export type DrawnLink = { a: number; b: number; sim: number; cluster: number }

/** Every link both of whose memories are drawn, once each, from each memory's
 *  `links` (`[other id, cosine]`). */
export function drawnLinks(items: { id: string; cluster: number; links?: [string, number][] }[]): DrawnLink[] {
  const at = new Map(items.map((n, i) => [n.id, i]))
  const out: DrawnLink[] = []
  items.forEach((n, i) => {
    for (const [other, sim] of n.links ?? []) {
      const j = at.get(other)
      if (j === undefined || j <= i) continue
      out.push({ a: i, b: j, sim, cluster: n.cluster })
    }
  })
  return out
}

/** Most links drawn at once. Measured on the 23k-memory store (54k links) in
 *  software WebGL: drawing every link stalled frames 0.5 to 1 s; the view's
 *  strongest few thousand do not. */
export const LINK_BUDGET = 6000

/**
 * The links worth drawing for the view: both memories inside `box` (the view,
 * with a margin), the closest pairs first, at most `budget` of them.
 */
export function linksInView(links: DrawnLink[], items: { x: number; y: number }[], box: GraphBbox, budget = LINK_BUDGET): DrawnLink[] {
  const inside = (i: number) => { const p = items[i]; return p.x >= box[0] && p.x <= box[2] && p.y >= box[1] && p.y <= box[3] }
  const out = links.filter(l => inside(l.a) && inside(l.b))
  if (out.length <= budget) return out
  return out.sort((p, q) => q.sim - p.sim).slice(0, budget)
}

/** The share of a cluster's disc its dots should ink, and the floor that
 *  keeps a dot findable: the screen-saturation rule deepscatter uses. */
export const INK_TARGET = 0.6
export const MIN_INK_ALPHA = 0.18

/**
 * Dot opacity for a cluster of `count` dots of `dotPx` pixels drawn over a
 * disc `radiusPx` wide on screen: light where dots pile up, solid once they
 * spread apart as the view zooms in.
 */
export function inkAlpha(count: number, dotPx: number, radiusPx: number): number {
  if (count <= 0) return 1
  const ink = count * Math.PI * (dotPx / 2) ** 2
  return Math.min(1, Math.max(MIN_INK_ALPHA, (INK_TARGET * Math.PI * radiusPx ** 2) / ink))
}

/** Labels with the whole store in view; one more each time the view zooms in
 *  2x, up to the most, so the overview stays calm. */
export const OVERVIEW_LABELS = 6
export const MAX_LABELS = 12

/** How many labels to draw at `zoom` (1 = the whole store in view). */
export const labelBudget = (zoom: number) => Math.min(MAX_LABELS, OVERVIEW_LABELS + Math.floor(Math.log2(Math.max(1, zoom))))

/** A memory's dot, in CSS pixels: 3 with the whole store in view, so dense
 *  topics read as dots, not blobs, growing 1.5 px per 2x zoom to 12 by 64x,
 *  where a fact ●, lesson ■ and experience ▲ are plain to tell apart. */
export const NODE_MIN_PX = 3
export const NODE_MAX_PX = 12

/** The dot size at `zoom` (1 = the whole store in view). */
export const dotPxAt = (zoom: number) => Math.min(NODE_MAX_PX, NODE_MIN_PX + 1.5 * Math.log2(Math.max(1, zoom)))

/** How much closer than "the whole store in view" the view is (1 = fitted). */
export function zoomFromFit(view: View, size: Size, bounds: GraphBbox | undefined): number {
  return bounds ? Math.max(1, view.k / fitBox(bounds, size).k) : 1
}

export type Dir = 'left' | 'right' | 'up' | 'down'

/**
 * The point to move keyboard focus to from `from` in direction `dir`: the
 * nearest one in that half-plane, favouring points straight ahead. Null when
 * nothing lies that way.
 */
export function nextInDirection(points: { id: string; at: Point }[], from: Point, dir: Dir): string | null {
  let best: string | null = null, bestScore = Infinity
  for (const p of points) {
    const dx = p.at.x - from.x, dy = p.at.y - from.y
    const ahead = dir === 'right' ? dx : dir === 'left' ? -dx : dir === 'down' ? dy : -dy
    const side = dir === 'right' || dir === 'left' ? Math.abs(dy) : Math.abs(dx)
    if (ahead <= 0.5) continue
    const score = ahead + 2 * side
    if (score < bestScore) { bestScore = score; best = p.id }
  }
  return best
}
