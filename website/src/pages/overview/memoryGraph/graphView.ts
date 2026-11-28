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
/** Deepest zoom, in screen px per layout unit. The server packs a cluster at
 *  about 20 square units a memory (memory_graph_layout: disc radius 2.5 *
 *  sqrt(count)), so neighbours sit about 4.5 units apart and at 16 px a unit
 *  about 70 px apart: a phone's view still holds dozens of memories. Deeper
 *  zooms only showed the empty space between two dots, which read as a blank
 *  canvas on a phone. */
export const MAX_K = 16

/** The golden ratio. The view's levels step by PHI ** 2 of zoom (about 2.6x):
 *  a memory's dot grows PHI times per step, and the links change what they join
 *  at each step (linkLevel), so every change of detail shares one rhythm with
 *  the server's golden-angle layout. */
export const PHI = (1 + Math.sqrt(5)) / 2
export const LEVEL_STEP = PHI * PHI
/** Levels of zoom past "the whole store in view": 0 at the fit, 1 at PHI ** 2. */
export const zoomLevel = (zoom: number) => Math.log(Math.max(1, zoom)) / Math.log(LEVEL_STEP)
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

/** Slide the view by a screen delta, keeping its centre over the layout's
 *  `bounds` when given, so a pan never leaves the view over nothing. */
export const panBy = (view: View, d: Point, bounds?: GraphBbox): View => {
  const cx = view.cx + d.x / view.k, cy = view.cy - d.y / view.k
  if (!bounds) return { ...view, cx, cy }
  return { ...view, cx: Math.min(bounds[2], Math.max(bounds[0], cx)), cy: Math.min(bounds[3], Math.max(bounds[1], cy)) }
}

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
export type LabelCandidate = { id: string; at: Point; text: string; rank: number; px?: number; color?: string; centred?: boolean; mark?: number
  /** The label's lines when it wraps (a topic's name, topicLines); one line when unset. */
  lines?: string[]
 }
export type PlacedLabel = LabelCandidate & { box: { x: number; y: number; width: number; height: number } }

/**
 * A memory's short title on the canvas, whole, never cut: a fact by its key's
 * last part (`pref.kirocrew.cli_help_probe` reads "cli help probe"), not its
 * whole `key = value`; a lesson or an experience by its first clause, up to the
 * first sentence end, colon, semicolon, dash or comma. Opening it shows the full text.
 */
const LEADING_STAMP = /^(?:[\d][\d\-/:.,T ]*(?:\s?(?:[AP]M|UTC|GMT|Z|[A-Z][DS]T))?\s*[:\u2014\u2013-]?\s+)+/u
export function canvasLabel(node: { kind: string; label: string; record?: { id: string } }): string {
  if (node.kind === 'fact' && node.record?.id) {
    const last = node.record.id.split('.').filter(Boolean).pop() ?? node.record.id
    const words = last.replace(/[_-]+/g, ' ').trim()
    if (words) return words
  }
  const flat = node.label.replace(/\s+/g, ' ').trim()
  // A leading date or time ("2026-08-13 14:05 PDT:") names nothing: it is skipped.
  const body = flat.replace(LEADING_STAMP, '') || flat
  const clause = body.split(/(?<=[.;:!?\u3002\uff1b\uff1a\uff01\uff1f])\s*|\s[\u2014\u2013-]\s|,\s|\uff0c/u)
    .map(part => part.trim()).find(part => (part.match(/\p{L}/gu)?.length ?? 0) >= 3)
  return (clause ?? body).replace(/[.:;,]+$/u, '')
}

/** A memory's title: at most MEMORY_LINES lines of MEMORY_LINE_PX each (about 22
 *  characters at 12 px). */
export const MEMORY_LINE_PX = 160
export const MEMORY_LINES = 2

/**
 * A memory's title over at most MEMORY_LINES lines no wider than `maxPx`
 * (`widthOf` measures a line, its padding included), broken between words, or
 * between characters in text with no spaces (CJK). Null when it does not fit:
 * the canvas then leaves that memory unnamed rather than cut its title.
 */
export function memoryLines(title: string, widthOf: (line: string) => number, maxPx = MEMORY_LINE_PX): string[] | null {
  // An already shortened source is not a whole title; removing its ellipsis
  // would disguise the cut as a complete word.
  if (title.includes('…')) return null
  const spaced = /\s/.test(title)
  const units = spaced ? title.split(/\s+/).filter(Boolean) : Array.from(title)
  const join = spaced ? ' ' : ''
  const lines: string[] = []
  for (const unit of units) {
    const last = lines.length ? lines[lines.length - 1] : null
    if (last !== null && widthOf(last + join + unit) <= maxPx) lines[lines.length - 1] = last + join + unit
    else if (widthOf(unit) <= maxPx && lines.length < MEMORY_LINES) lines.push(unit)
    else return null
  }
  return lines.length ? lines : null
}

/** A topic's name: at most TOPIC_LINES lines of TOPIC_LINE_PX each (about 18
 *  characters at 13 px), each line TOPIC_LINE_HEIGHT times the text size. */
export const TOPIC_LINE_PX = 150
/** On a phone-wide canvas (under 600 px) lines are narrower, so names wrap and
 *  two or three fit side by side across the view. */
export const TOPIC_LINE_PX_PHONE = 96
export const topicLinePx = (width: number) => (width < 600 ? TOPIC_LINE_PX_PHONE : TOPIC_LINE_PX)
export const TOPIC_LINES = 2
export const TOPIC_LINE_HEIGHT = 1.2

/**
 * A topic's name in whole words over at most TOPIC_LINES lines no wider than
 * `maxPx` (`widthOf` measures a line, its padding included). A name that does
 * not fit drops its last word (and an "&" left at the end) until it does: it
 * is never cut inside a word or marked with "…". A single word too wide for a
 * line is still shown whole, on its own line.
 */
export function topicLines(text: string, widthOf: (line: string) => number, maxPx = TOPIC_LINE_PX): string[] {
  let words = text.split(/\s+/).filter(Boolean)
  while (words.length > 1) {
    const lines: string[] = []
    let ok = true
    for (const word of words) {
      const last = lines.length ? lines[lines.length - 1] : null
      if (last !== null && widthOf(`${last} ${word}`) <= maxPx) lines[lines.length - 1] = `${last} ${word}`
      else if (widthOf(word) <= maxPx && lines.length < TOPIC_LINES) lines.push(word)
      else { ok = false; break }
    }
    if (ok) return lines
    words = words.slice(0, -1)
    while (words.length > 1 && words[words.length - 1] === '&') words = words.slice(0, -1)
  }
  return words
}

/**
 * The order topic names are placed in (index i is `clusters[i]`'s place, 0
 * first). The server lays topics out on a golden-angle spiral whose slot i sits
 * at radius sqrt(i), so walking out from a point in order of distance walks the
 * spiral's slots. With the whole store in view (below the first zoomLevel) the
 * walk starts at the layout's middle, so the names that fit spread over the
 * whole map; zoomed in it starts at the topic nearest the middle of the screen,
 * so the place being looked at is named first.
 */
export function topicOrder(clusters: { x: number; y: number }[], view: View, size: Size, bounds: GraphBbox | undefined, zoom: number): number[] {
  let from: Point = bounds ? { x: (bounds[0] + bounds[2]) / 2, y: (bounds[1] + bounds[3]) / 2 } : { x: 0, y: 0 }
  if (zoomLevel(zoom) >= 1 && clusters.length) {
    const mid = toLayout(view, size, { x: size.width / 2, y: size.height / 2 })
    from = clusters.reduce((best, c) => Math.hypot(c.x - mid.x, c.y - mid.y) < Math.hypot(best.x - mid.x, best.y - mid.y) ? c : best, clusters[0])
  }
  const byDistance = clusters.map((c, i) => ({ i, d: Math.hypot(c.x - from.x, c.y - from.y) })).sort((a, b) => a.d - b.d || a.i - b.i)
  const order = new Array<number>(clusters.length)
  byDistance.forEach(({ i }, k) => { order[i] = k })
  return order
}

/** The grid that spreads topic names: at most one per cell. */
export type LabelGrid = { cols: number; rows: number }
/** A grid whose cells are about 2 name widths by 3 name heights, with at least
 *  3 columns: with 2, the names nearest the middle (placed first) fill both
 *  columns' inner edges and the left and right thirds stay bare. */
export const labelGrid = (size: Size, nameWidth: number, nameHeight: number): LabelGrid => ({
  cols: Math.max(3, Math.round(size.width / (2 * nameWidth))),
  rows: Math.max(2, Math.round(size.height / (3 * nameHeight))),
})

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
export function placeLabels(cands: LabelCandidate[], size: Size, max: number, widthOf: (text: string, px: number, bold?: boolean) => number, marks: ScreenMark[] = [], grid?: LabelGrid, avoid: LabelBox[] = []): PlacedLabel[] {
  const out: PlacedLabel[] = []
  // Screen areas no label may cover (the zoom controls), held like placed labels.
  const held = avoid.map(box => ({ box }))
  const seen = new Set<string>()
  // One topic name per grid cell (by its anchor), so names cover the whole view.
  const taken = new Set<number>()
  const cellOf = (at: Point) => grid ? Math.floor(at.y / (size.height / grid.rows)) * grid.cols + Math.floor(at.x / (size.width / grid.cols)) : -1
  const sorted = [...cands].sort((a, b) => b.rank - a.rank || (a.id < b.id ? -1 : 1))
  const g = LABEL_GAP
  const fits = (box: LabelBox) => box.x >= 0 && box.y >= 0 && box.x + box.width <= size.width && box.y + box.height <= size.height
    && ![...held, ...out].some(o => box.x < o.box.x + o.box.width + g && o.box.x < box.x + box.width + g && box.y < o.box.y + o.box.height + g && o.box.y < box.y + box.height + g)
  for (const c of sorted) {
    if (out.length >= max) break
    if (seen.has(c.text) || c.text.includes('…')) continue
    const px = c.px ?? LABEL_PX
    const lines = c.lines ?? [c.text]
    if (!lines.length || lines.some(line => line.includes('…'))) continue
    // A topic's name (centred) is semibold, a memory's regular.
    const width = Math.max(...lines.map(line => widthOf(line, px, c.centred)))
    const height = lines.length === 1 ? Math.round(px * 1.5) : Math.round(px * TOPIC_LINE_HEIGHT * lines.length + px * 0.5)
    let box: LabelBox | undefined
    if (c.mark !== undefined) {
      const y = c.at.y - height / 2, reach = c.mark / 2 + MARK_GAP - LABEL_PAD
      const sides = [{ x: c.at.x + reach, y, width, height }, { x: c.at.x - reach - width, y, width, height }]
        .filter(fits).map(b => ({ b, covered: marksUnder(b, marks, c.id) }))
        .sort((p, q) => p.covered - q.covered)
      if (sides.length && sides[0].covered <= MAX_COVERED_MARKS) box = sides[0].b
    } else if (c.centred) {
      // A topic's name is centred on its disc; one whose disc centre is on
      // screen but near an edge slides in just far enough to show whole.
      if (c.at.x < 0 || c.at.y < 0 || c.at.x > size.width || c.at.y > size.height) continue
      if (grid && taken.has(cellOf(c.at))) continue
      const x = Math.min(Math.max(c.at.x - width / 2, 0), size.width - width)
      const y = Math.min(Math.max(c.at.y - height / 2, 0), size.height - height)
      const b = { x, y, width, height }
      if (fits(b)) box = b
    } else {
      const b = { x: c.at.x - width / 2, y: c.at.y - height - 6, width, height }
      if (fits(b)) box = b
    }
    if (!box) continue
    if (grid && c.centred) taken.add(cellOf(c.at))
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

/** A drawn link: point indices `a < b`, its cosine, and the cluster its colour
 *  comes from. `hub` marks a link between two topics (topicLinks), its `sim`
 *  then the share of the busiest such link it carries. */
export type DrawnLink = { a: number; b: number; sim: number; cluster: number; hub?: boolean }

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
  // Closest first, sorted once here, so a view's pick needs no sort of its own.
  return out.sort((p, q) => q.sim - p.sim)
}

/** Most links drawn at once. Measured on the 23k-memory store (54k links) in
 *  software WebGL: drawing every link stalled frames 0.5 to 1 s; the view's
 *  strongest few thousand do not. */
export const LINK_BUDGET = 6000

/**
 * The links worth drawing for the view: both memories inside `box` (the view,
 * with a margin), at most `budget` of them. `links` come closest first
 * (`drawnLinks`), so the first `budget` inside are the closest pairs and the
 * walk stops there: one pass, no sort, on every settle of the view.
 */
export function linksInView(links: DrawnLink[], items: { x: number; y: number; cluster?: number }[], box: GraphBbox, budget = LINK_BUDGET, rule?: LinkRule): DrawnLink[] {
  const inside = (i: number) => { const p = items[i]; return p.x >= box[0] && p.x <= box[2] && p.y >= box[1] && p.y <= box[3] }
  const out: DrawnLink[] = []
  const per = rule ? new Uint8Array(items.length) : null
  const max2 = rule ? rule.maxLen * rule.maxLen : 0
  for (const l of links) {
    if (!inside(l.a) || !inside(l.b)) continue
    if (rule && per) {
      if (per[l.a] >= rule.perNode || per[l.b] >= rule.perNode) continue
      const p = items[l.a], q = items[l.b]
      if (p.cluster !== q.cluster && (!rule.across || (p.x - q.x) ** 2 + (p.y - q.y) ** 2 > max2)) continue
      per[l.a]++; per[l.b]++
    }
    out.push(l)
    if (out.length >= budget) break
  }
  return out
}

/** Which links a level draws: each memory's `perNode` closest, inside its own
 *  topic, and (`across`) to another topic only when shorter than `maxLen`
 *  layout units. */
export type LinkRule = { perNode: number; across: boolean; maxLen: number }
/** Links a memory keeps at most, closest first: more turned a topic into a hairball. */
export const LINKS_PER_NODE = 2

/**
 * What links the view draws at `zoom` (1 = the whole store in view), changing
 * at the same PHI ** 2 steps as the dots:
 * - `topics` below one step: no memory's own links, only one link between two
 *   neighbouring topics' centres, as strong as the links between them;
 * - `within` to the second step: links inside a topic only;
 * - `near` past it: also links to a neighbouring topic, when short on screen.
 */
export type LinkLevel = 'topics' | 'within' | 'near'
export const linkLevel = (zoom: number): LinkLevel => zoomLevel(zoom) < 1 ? 'topics' : zoomLevel(zoom) < 2 ? 'within' : 'near'

/** The rule for a memory-level link at `level`, for a view of `k` px a layout unit. */
export const linkRule = (level: LinkLevel, k: number, farPx: number): LinkRule =>
  ({ perNode: LINKS_PER_NODE, across: level === 'near', maxLen: farPx / k })

/** Links each topic keeps to its neighbours at the `topics` level. */
export const TOPIC_LINKS = 2

/**
 * One link between each two topics that sit side by side on the map and share
 * links, each topic keeping its `TOPIC_LINKS` busiest. A link joins the two
 * memories nearest their topics' centres (the server puts a topic's most
 * typical memory there), so it needs no point of its own. Side by side: the
 * gap between the discs is under the larger radius. `sim` is the link's count
 * as a share of the busiest one's.
 */
export function topicLinks(links: DrawnLink[], items: { x: number; y: number; cluster: number }[], clusters: GraphCluster[]): DrawnLink[] {
  const at = new Map(clusters.map((c, i) => [c.id, i]))
  const n = clusters.length
  // Each topic's memory nearest its centre.
  const hub = new Int32Array(n).fill(-1), hubD = new Float64Array(n).fill(Infinity)
  items.forEach((p, idx) => {
    const i = at.get(p.cluster)
    if (i === undefined) return
    const d = (p.x - clusters[i].x) ** 2 + (p.y - clusters[i].y) ** 2
    if (d < hubD[i]) { hubD[i] = d; hub[i] = idx }
  })
  const counts = new Map<number, number>()
  for (const l of links) {
    const i = at.get(items[l.a].cluster), j = at.get(items[l.b].cluster)
    if (i === undefined || j === undefined || i === j) continue
    const key = Math.min(i, j) * n + Math.max(i, j)
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  const pairs = [...counts.entries()].map(([key, count]) => ({ i: Math.floor(key / n), j: key % n, count }))
    .filter(({ i, j }) => {
      const p = clusters[i], q = clusters[j]
      return Math.hypot(p.x - q.x, p.y - q.y) - p.r - q.r < Math.max(p.r, q.r)
    })
    .sort((p, q) => q.count - p.count || p.i - q.i || p.j - q.j)
  const per = new Uint8Array(n), top = pairs[0]?.count ?? 1
  const out: DrawnLink[] = []
  for (const { i, j, count } of pairs) {
    if (per[i] >= TOPIC_LINKS || per[j] >= TOPIC_LINKS) continue
    per[i]++; per[j]++
    out.push({ a: Math.min(hub[i], hub[j]), b: Math.max(hub[i], hub[j]), sim: count / top, cluster: clusters[i].id, hub: true })
  }
  return out
}

/** A topic link's opacity for its share of the busiest one. */
export const topicLinkAlpha = (share: number) => 0.12 + 0.28 * Math.min(1, Math.max(0, share))

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
 *  PHI times (half a level), up to the most, so the overview stays calm. */
export const OVERVIEW_LABELS = 6
export const MAX_LABELS = 12

/** How many labels to draw at `zoom` (1 = the whole store in view). */
export const labelBudget = (zoom: number) => Math.min(MAX_LABELS, OVERVIEW_LABELS + Math.floor(2 * zoomLevel(zoom)))

/** A memory's dot, in CSS pixels: 3 with the whole store in view, so dense
 *  topics read as dots, not blobs, growing PHI times per level (zoomLevel) to
 *  12 by the third (about 18x), where a fact ●, lesson ■ and experience ▲ are
 *  plain to tell apart. */
export const NODE_MIN_PX = 3
export const NODE_MAX_PX = 12

/** The dot size at `zoom` (1 = the whole store in view). */
export const dotPxAt = (zoom: number) => Math.min(NODE_MAX_PX, NODE_MIN_PX * PHI ** zoomLevel(zoom))

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
