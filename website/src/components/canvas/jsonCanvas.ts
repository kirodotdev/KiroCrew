/**
 * The canvas file format: a JSON Canvas 1.0 document (https://jsoncanvas.org/spec/1.0/)
 * with Kiro's own fields added, saved with the `.canvas` extension.
 *
 * `{ "kirocanvas": 1, "nodes": [...], "edges": [...] }`
 *
 * Every shape is a JSON Canvas `text` node (a section is a `group`) carrying the
 * standard fields any JSON Canvas reader understands: id, position, size, text
 * and one colour. What JSON Canvas has no word for (the shape's kind, its named
 * colour, fill, outline, type style, lock; a line's style, dash and route) rides
 * along as added fields, which Obsidian keeps when it saves. A document written
 * elsewhere still reads:
 *
 * - `color` wins over `swatch` when the two disagree, since an editor that does
 *   not know `swatch` leaves it stale; presets `"1"`-`"6"` map to the nearest swatch.
 * - a text node without `shape` is a rectangle; a group without one is a section.
 * - a missing `toEnd` is an arrow and a missing `fromEnd` none, the spec's defaults.
 * - fields Kiro does not use, and nodes of a type it cannot draw (`file`, `link`),
 *   are kept and written back unchanged, so an autosave never drops them.
 *
 * Array order is stacking order, back to front, as the spec says.
 */

import { toHex } from './colorMath'
import {
  CANVAS_KIND, CANVAS_SCHEMA, DEFAULT_SIZE, SWATCH_STYLE, SWATCHES, alignOf, fontSizeOf, lookOf, newId, textStyleOf, toSwatch,
  type Canvas, type Connector, type ConnectorArrows, type ConnectorLine, type ConnectorSide, type Shape, type ShapeKind, type Swatch,
} from './canvasModel'

/** The top-level field that marks a document as one Kiro wrote, and its version. */
export const FORMAT_MARK = 'kirocanvas'
export const FORMAT_VERSION = 1

const KINDS = new Set<ShapeKind>(['sticky', 'rect', 'ellipse', 'diamond', 'text', 'section'])
const SIDES = new Set<ConnectorSide>(['top', 'right', 'bottom', 'left'])
const LINES = new Set<ConnectorLine>(['elbow', 'straight', 'curve'])

/** JSON Canvas's six preset colours, as the swatches closest to them. */
const PRESETS: Record<string, Swatch> = { 1: 'red', 2: 'orange', 3: 'yellow', 4: 'green', 5: 'teal', 6: 'violet' }

/** The swatch for a preset colour, read from PRESETS' own keys so `"constructor"` is not one. */
const presetOf = (color: unknown): Swatch | undefined =>
  typeof color === 'string' && Object.hasOwn(PRESETS, color) ? PRESETS[color] : undefined

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => !!v && typeof v === 'object' && !Array.isArray(v)
const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)
const drawable = (o: Obj) => o.type === 'text' || o.type === 'group'

const rgb = (hex: string) => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16))

/** The swatch closest to *hex* by either of its shades, for a line coloured elsewhere. */
function nearestSwatch(hex: string, preferred?: Swatch): Swatch {
  const [r, g, b] = rgb(hex)
  let best: Swatch = SWATCHES[0]
  let dist = Infinity
  for (const s of preferred ? [preferred, ...SWATCHES] : SWATCHES) {
    for (const shade of [SWATCH_STYLE[s].stroke, SWATCH_STYLE[s].fill]) {
      const [r2, g2, b2] = rgb(shade)
      const d = (r - r2) ** 2 + (g - g2) ** 2 + (b - b2) ** 2
      if (d < dist) { dist = d; best = s }
    }
  }
  return best
}

/** A shape's fill as one hex value, the colour a plain JSON Canvas reader shows. */
const fillHex = (s: Pick<Shape, 'color' | 'hex'>) => s.hex ?? SWATCH_STYLE[s.color].fill

/** A node's colour fields: the named swatch and custom hex, with `color` winning when it disagrees. */
function nodeColour(o: Obj): Pick<Shape, 'color'> & { hex?: string } {
  const swatch = toSwatch(o.swatch) ?? 'white'
  const hex = toHex(o.hex)
  const color = o.color
  const preset = presetOf(color)
  if (preset) return { color: preset }
  const written = toHex(color)
  if (!written) return { color: swatch, ...(hex ? { hex } : {}) }
  const expected = hex ?? SWATCH_STYLE[swatch].fill
  if (written.toUpperCase() === expected.toUpperCase()) return { color: swatch, ...(hex ? { hex } : {}) }
  const named = SWATCHES.find(s => SWATCH_STYLE[s].fill.toUpperCase() === written.toUpperCase())
  return named ? { color: named } : { color: swatch, hex: written }
}

function readShape(o: Obj, id: string): Shape {
  const kind: ShapeKind = KINDS.has(o.shape as ShapeKind) ? (o.shape as ShapeKind) : o.type === 'group' ? 'section' : 'rect'
  const text = kind === 'section' ? (typeof o.label === 'string' ? o.label : '') : (typeof o.text === 'string' ? o.text : '')
  const { color, hex } = nodeColour(o)
  const look = lookOf({ ...o, hex })
  return {
    id, kind,
    x: num(o.x, 0), y: num(o.y, 0),
    w: Math.max(24, num(o.width, DEFAULT_SIZE[kind].w)), h: Math.max(24, num(o.height, DEFAULT_SIZE[kind].h)),
    text,
    color,
    ...(o.locked === true ? { locked: true } : {}),
    ...look,
    ...(kind === 'text' ? textStyleOf(o) : {}),
    ...alignOf(kind, o.align),
    ...fontSizeOf(kind, o.fontSize),
  }
}

/** A line's arrows from JSON Canvas's two ends; *swap* says the line was drawn backwards. */
function arrowsOf(fromEnd: unknown, toEnd: unknown): { arrows?: ConnectorArrows; swap: boolean } {
  const start = fromEnd === 'arrow'
  const end = toEnd !== 'none'
  if (start && end) return { arrows: 'both', swap: false }
  if (start) return { swap: true }
  return end ? { swap: false } : { arrows: 'none', swap: false }
}

function readConnector(c: Obj, id: string): Connector {
  const { arrows, swap } = arrowsOf(c.fromEnd, c.toEnd)
  const from = (swap ? c.toNode : c.fromNode) as string
  const to = (swap ? c.fromNode : c.toNode) as string
  const fromSide = swap ? c.toSide : c.fromSide
  const toSide = swap ? c.fromSide : c.toSide
  const conn: Connector = { id, from, to }
  if (SIDES.has(fromSide as ConnectorSide)) conn.fromSide = fromSide as ConnectorSide
  if (SIDES.has(toSide as ConnectorSide)) conn.toSide = toSide as ConnectorSide
  if (LINES.has(c.line as ConnectorLine)) conn.line = c.line as ConnectorLine
  const swatch = toSwatch(c.swatch)
  const color = c.color
  const written = toHex(color)
  const preset = presetOf(color)
  if (preset) conn.color = preset
  else if (written) conn.color = nearestSwatch(written, swatch)
  else if (swatch) conn.color = swatch
  if (c.dashed === true) conn.dashed = true
  if (arrows) conn.arrows = arrows
  const route = Array.isArray(c.route) ? (c.route as unknown[]) : []
  if (route.length && route.every(p => isObj(p) && Number.isFinite(p.x) && Number.isFinite(p.y))) {
    conn.route = (route as Obj[]).map(p => ({ x: p.x as number, y: p.y as number }))
    if (swap) conn.route.reverse()
  }
  return conn
}

/**
 * A canvas from a JSON Canvas document, or null when *data* is not one. Shapes
 * and lines it cannot read are kept for writing back, not dropped.
 */
export function fromJsonCanvas(data: unknown): Canvas | null {
  if (!isObj(data)) return null
  if (data.nodes !== undefined && !Array.isArray(data.nodes)) return null
  if (data.edges !== undefined && !Array.isArray(data.edges)) return null
  if (data.nodes === undefined && data.edges === undefined && data[FORMAT_MARK] === undefined && Object.keys(data).length) return null
  if (!Object.keys(data).length) return { kind: CANVAS_KIND, schema: CANVAS_SCHEMA, shapes: [], connectors: [] }
  const shapes: Shape[] = []
  const nodes: Record<string, Obj> = Object.create(null)
  const edges: Record<string, Obj> = Object.create(null)
  const nodeOrder: string[] = []
  const edgeOrder: string[] = []
  // Every id, including a foreign node's, occupies one source slot.
  const collect = (values: unknown[], entries: Record<string, Obj>, order: string[], prefix: string) => {
    const reserved = new Set(values.filter(isObj).map(o => o.id))
    for (const o of values) {
      if (!isObj(o) || typeof o.id !== 'string') continue
      let id = o.id
      if (Object.hasOwn(entries, id)) {
        do { id = newId(prefix) } while (reserved.has(id))
      }
      reserved.add(id)
      entries[id] = id === o.id ? o : { ...o, id }
      order.push(id)
    }
  }
  collect((data.nodes as unknown[] | undefined) ?? [], nodes, nodeOrder, 's')
  for (const id of nodeOrder) if (drawable(nodes[id])) shapes.push(readShape(nodes[id], id))
  const ours = new Set(shapes.map(s => s.id))
  const connectors: Connector[] = []
  collect((data.edges as unknown[] | undefined) ?? [], edges, edgeOrder, 'c')
  for (const id of edgeOrder) {
    const e = edges[id]
    if (ours.has(e.fromNode as string) && ours.has(e.toNode as string)) connectors.push(readConnector(e, id))
  }
  const { nodes: _nodes, edges: _edges, ...top } = data
  void _nodes; void _edges
  return {
    kind: CANVAS_KIND, schema: CANVAS_SCHEMA, shapes, connectors,
    source: freeze({ nodes, edges, nodeOrder, edgeOrder, top }),
  }
}

/** Keep source nesting within the stack budget of JSON writes and editor snapshot comparisons. */
const MAX_SOURCE_DEPTH = 100

/** Source values are immutable, including nested extension data shared by snapshots. */
function freeze<T>(value: T, depth = 0): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    // parseCanvas catches this and reports the document as unreadable; nothing shows the error.
    if (depth > MAX_SOURCE_DEPTH) throw new RangeError()
    for (const child of Object.values(value)) freeze(child, depth + 1)
    Object.freeze(value)
  }
  return value
}

function writeShape(s: Shape): Obj {
  const section = s.kind === 'section'
  return {
    id: s.id,
    type: section ? 'group' : 'text',
    x: Math.round(s.x), y: Math.round(s.y), width: Math.round(s.w), height: Math.round(s.h),
    ...(section ? { label: s.text } : { text: s.text }),
    color: fillHex(s),
    shape: s.kind,
    swatch: s.color,
    ...(s.hex ? { hex: s.hex } : {}),
    ...(s.fill ? { fill: s.fill } : {}),
    ...(s.weight ? { weight: s.weight } : {}),
    ...(s.border ? { border: s.border } : {}),
    ...(s.borderColor ? { borderColor: s.borderColor } : {}),
    ...(s.borderHex ? { borderHex: s.borderHex } : {}),
    ...(s.ink ? { ink: s.ink } : {}),
    ...(s.inkHex ? { inkHex: s.inkHex } : {}),
    ...(s.fontSize !== undefined ? { fontSize: s.fontSize } : {}),
    ...(s.bold ? { bold: true } : {}),
    ...(s.strike ? { strike: true } : {}),
    ...(s.align ? { align: s.align } : {}),
    ...(s.locked ? { locked: true } : {}),
  }
}

function writeConnector(c: Connector): Obj {
  const arrows = c.arrows ?? 'end'
  return {
    id: c.id,
    fromNode: c.from,
    ...(c.fromSide ? { fromSide: c.fromSide } : {}),
    fromEnd: arrows === 'both' ? 'arrow' : 'none',
    toNode: c.to,
    ...(c.toSide ? { toSide: c.toSide } : {}),
    toEnd: arrows === 'none' ? 'none' : 'arrow',
    ...(c.color ? { color: SWATCH_STYLE[c.color].stroke, swatch: c.color } : {}),
    ...(c.line ? { line: c.line } : {}),
    ...(c.dashed ? { dashed: true } : {}),
    ...(c.route ? { route: c.route } : {}),
  }
}

/** Canonical model baselines live outside the JSON-safe source and are computed once per entry. */
const baselines = new WeakMap<Obj, Obj>()
function preserve(raw: Obj | undefined, now: Obj, baseline: (o: Obj) => Obj, orient = false): Obj {
  if (!raw) return now
  let base = baselines.get(raw)
  if (!base) { base = baseline(raw); baselines.set(raw, base) }
  const changed = Object.keys(now).filter(k => JSON.stringify(now[k]) !== JSON.stringify(base[k]))
  const removed = Object.keys(base).filter(k => !Object.hasOwn(now, k))
  if (!changed.length && !removed.length) return raw
  const out = { ...raw, ...Object.fromEntries(changed.map(k => [k, now[k]])) }
  for (const k of removed) delete out[k]
  // A reversed edge's endpoints, arrows and route share one coordinate orientation.
  if (orient) {
    for (const k of ['fromNode', 'toNode', 'fromSide', 'toSide', 'fromEnd', 'toEnd', 'route']) {
      if (Object.hasOwn(now, k)) out[k] = now[k]
      else delete out[k]
    }
  }
  return out
}

/** Models fill surviving drawable slots in stacking order; foreign slots stay put. */
function ordered<T extends { id: string }>(
  entries: Readonly<Record<string, Obj>>, order: readonly string[], models: T[],
  isDrawable: (o: Obj) => boolean, write: (model: T, raw?: Obj) => Obj, keep: (o: Obj) => boolean = () => true,
): Obj[] {
  const ids = new Set(models.map(m => m.id))
  const out: Obj[] = []
  let index = 0
  for (const id of order) {
    const raw = entries[id]
    if (!isDrawable(raw)) { if (keep(raw)) out.push(raw) }
    else if (ids.has(id)) {
      const model = models[index++]
      out.push(write(model, entries[model.id]))
    }
  }
  for (; index < models.length; index++) {
    const model = models[index]
    out.push(write(model, entries[model.id]))
  }
  return out
}

/** Untouched values come from the file; only model changes replace or remove keys. */
export function toJsonCanvas(c: Canvas): Obj {
  const source = c.source
  if (!source) return { [FORMAT_MARK]: FORMAT_VERSION, nodes: c.shapes.map(writeShape), edges: c.connectors.map(writeConnector) }
  const nodes = ordered(source.nodes, source.nodeOrder, c.shapes, drawable,
    (s, raw) => preserve(raw, writeShape(s), o => writeShape(readShape(o, s.id))))
  const nodeIds = new Set(nodes.map(n => n.id))
  const survives = (e: Obj) => nodeIds.has(e.fromNode) && nodeIds.has(e.toNode)
  const wasDrawable = (e: Obj) => [e.fromNode, e.toNode].every(id =>
    typeof id === 'string' && Object.hasOwn(source.nodes, id) && drawable(source.nodes[id]))
  const edges = ordered(source.edges, source.edgeOrder,
    c.connectors.filter(e => nodeIds.has(e.from) && nodeIds.has(e.to)), wasDrawable,
    (e, raw) => preserve(raw, writeConnector(e), o => writeConnector(readConnector(o, e.id)), !!raw && arrowsOf(raw.fromEnd, raw.toEnd).swap), survives)
  return { ...source.top, nodes, edges }
}
