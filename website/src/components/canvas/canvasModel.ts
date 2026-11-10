/**
 * A canvas artifact's data: shapes, the connectors between them, and the pure
 * helpers the editor uses to read, hit-test and lay them out.
 *
 * On disk a canvas is one JSON document (the `.kcanvas` format):
 * `{ "kind": "kirocrew-canvas", "schema": 1, "shapes": [...], "connectors": [...] }`.
 * Reading never throws: an unknown value is dropped, and a document that is not
 * a canvas at all is reported as unreadable, so the editor can refuse to save
 * over it rather than replace it with an empty canvas.
 */

import { toHex } from './colorMath'

export const CANVAS_KIND = 'kirocrew-canvas'
export const CANVAS_SCHEMA = 1

/** A `section` is a FigJam section: a titled container that carries what sits in it. */
export type ShapeKind = 'sticky' | 'rect' | 'ellipse' | 'diamond' | 'text' | 'section'
export type Swatch =
  | 'black' | 'dark' | 'red' | 'orange' | 'yellow' | 'green' | 'teal' | 'blue' | 'violet' | 'magenta' | 'white'
  | 'silver' | 'smoke' | 'rose' | 'peach' | 'cream' | 'mint' | 'ice' | 'sky' | 'lavender' | 'blush'

/** How a shape is filled: solid, see-through, or not at all (just the outline). */
export type FillMode = 'solid' | 'transparent' | 'none'
/** How heavy a shape's outline is. */
export type StrokeWeight = 'thin' | 'medium' | 'thick'
/** A shape's outline: solid, dashed, or none. */
export type BorderStyle = 'solid' | 'dashed' | 'none'

export interface Shape {
  id: string
  kind: ShapeKind
  x: number
  y: number
  w: number
  h: number
  text: string
  color: Swatch
  /** Locked shapes cannot be picked, moved or edited until unlocked. */
  locked?: boolean
  /** Unset is solid. */
  fill?: FillMode
  /** Unset is medium. */
  weight?: StrokeWeight
  /** Unset is solid. */
  border?: BorderStyle
  /** The outline's own colour; unset, it follows the fill's. */
  borderColor?: Swatch
  /** A custom fill colour, `#RRGGBB`. Overrides `color`; unset, the swatch is used. */
  hex?: string
  /** A custom outline colour, `#RRGGBB`. Overrides `borderColor`. */
  borderHex?: string
}

export type ConnectorSide = 'top' | 'right' | 'bottom' | 'left'
export type ConnectorLine = 'elbow' | 'straight' | 'curve'
export type ConnectorArrows = 'end' | 'both' | 'none'

/**
 * A line between two shapes. Everything past from/to is optional: an unpinned
 * side faces the other shape, the line is an elbow, the colour is the canvas's
 * ink, solid, with an arrow at the end.
 */
export interface Connector {
  id: string
  from: string
  to: string
  fromSide?: ConnectorSide
  toSide?: ConnectorSide
  line?: ConnectorLine
  color?: Swatch
  dashed?: boolean
  arrows?: ConnectorArrows
  /** An elbow's corners once someone has dragged its segments; unset, it routes itself. */
  route?: Pt[]
}

export interface Canvas {
  kind: typeof CANVAS_KIND
  schema: typeof CANVAS_SCHEMA
  shapes: Shape[]
  connectors: Connector[]
}

/** The strong row of FigJam's colour picker, then its light row. */
export const STRONG_SWATCHES: Swatch[] = ['black', 'dark', 'red', 'orange', 'yellow', 'green', 'teal', 'blue', 'violet', 'magenta', 'white']
export const LIGHT_SWATCHES: Swatch[] = ['silver', 'smoke', 'rose', 'peach', 'cream', 'mint', 'ice', 'sky', 'lavender', 'blush']
export const SWATCHES: Swatch[] = [...STRONG_SWATCHES, ...LIGHT_SWATCHES]

export { CONN_INK } from './canvasPaint'

/** A colour name from a file, or undefined if it is not one. */
export function toSwatch(v: unknown): Swatch | undefined {
  return typeof v === 'string' && SWATCHES.includes(v as Swatch) ? (v as Swatch) : undefined
}

const FILLS = new Set<FillMode>(['solid', 'transparent', 'none'])
const WEIGHTS = new Set<StrokeWeight>(['thin', 'medium', 'thick'])
const BORDERS = new Set<BorderStyle>(['solid', 'dashed', 'none'])
export const toFill = (v: unknown): FillMode | undefined => (FILLS.has(v as FillMode) ? (v as FillMode) : undefined)
export const toBorder = (v: unknown): BorderStyle | undefined => (BORDERS.has(v as BorderStyle) ? (v as BorderStyle) : undefined)
export const toWeight = (v: unknown): StrokeWeight | undefined => (WEIGHTS.has(v as StrokeWeight) ? (v as StrokeWeight) : undefined)

/**
 * Per swatch: a sticky's paper, a shape's fill and its border, and the ink that
 * reads on it. A strong colour fills with itself and borders in a deeper shade; a
 * light colour fills pale and borders in its strong sibling, as in FigJam. These
 * are the shapes' own paint, not the app's chrome, so they are literal colours
 * that look the same in every theme, like paper on a desk.
 */
export const SWATCH_STYLE: Record<Swatch, { paper: string; fill: string; stroke: string; ink: string }> = {
  black: { paper: '#1E1E1E', fill: '#1E1E1E', stroke: '#000000', ink: '#FFFFFF' },
  dark: { paper: '#757575', fill: '#757575', stroke: '#4D4D4D', ink: '#FFFFFF' },
  red: { paper: '#F2573C', fill: '#F2573C', stroke: '#C13A22', ink: '#1F2328' },
  orange: { paper: '#FFA23D', fill: '#FFA23D', stroke: '#D07A1C', ink: '#1F2328' },
  yellow: { paper: '#FFC83D', fill: '#FFC83D', stroke: '#E3A021', ink: '#1F2328' },
  green: { paper: '#6CD26E', fill: '#6CD26E', stroke: '#3CA03E', ink: '#1F2328' },
  teal: { paper: '#58D8CC', fill: '#58D8CC', stroke: '#2E9F96', ink: '#1F2328' },
  blue: { paper: '#3FAAFF', fill: '#3FAAFF', stroke: '#0B78D6', ink: '#1F2328' },
  violet: { paper: '#874FFF', fill: '#874FFF', stroke: '#5A2BD0', ink: '#FFFFFF' },
  magenta: { paper: '#F057C8', fill: '#F057C8', stroke: '#B72E91', ink: '#1F2328' },
  white: { paper: '#FFFFFF', fill: '#FFFFFF', stroke: '#6B6B6B', ink: '#1F2328' },
  silver: { paper: '#B3B3B3', fill: '#B3B3B3', stroke: '#757575', ink: '#1F2328' },
  smoke: { paper: '#D9D9D9', fill: '#D9D9D9', stroke: '#8A8A8A', ink: '#1F2328' },
  rose: { paper: '#F7C9C4', fill: '#F7C9C4', stroke: '#F2573C', ink: '#1F2328' },
  peach: { paper: '#FCE0C4', fill: '#FCE0C4', stroke: '#FFA23D', ink: '#1F2328' },
  cream: { paper: '#FFF0C2', fill: '#FFF0C2', stroke: '#FFC83D', ink: '#1F2328' },
  mint: { paper: '#D5F2D3', fill: '#D5F2D3', stroke: '#6CD26E', ink: '#1F2328' },
  ice: { paper: '#D2F6F4', fill: '#D2F6F4', stroke: '#58D8CC', ink: '#1F2328' },
  sky: { paper: '#CDE4FD', fill: '#CDE4FD', stroke: '#3FAAFF', ink: '#1F2328' },
  lavender: { paper: '#DCD0FA', fill: '#DCD0FA', stroke: '#874FFF', ink: '#1F2328' },
  blush: { paper: '#F6C6EB', fill: '#F6C6EB', stroke: '#F057C8', ink: '#1F2328' },
}

export const DEFAULT_SIZE: Record<ShapeKind, { w: number; h: number }> = {
  sticky: { w: 200, h: 200 },
  rect: { w: 220, h: 130 },
  ellipse: { w: 180, h: 130 },
  diamond: { w: 170, h: 150 },
  text: { w: 220, h: 40 },
  section: { w: 300, h: 360 },
}

export const emptyCanvas = (): Canvas => ({ kind: CANVAS_KIND, schema: CANVAS_SCHEMA, shapes: [], connectors: [] })

const KINDS = new Set<ShapeKind>(['sticky', 'rect', 'ellipse', 'diamond', 'text', 'section'])
const CONN_SIDES = new Set<ConnectorSide>(['top', 'right', 'bottom', 'left'])
const CONN_LINES = new Set<ConnectorLine>(['elbow', 'straight', 'curve'])
const CONN_ARROWS = new Set<ConnectorArrows>(['end', 'both', 'none'])
const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d)

export type ShapeLookFields = Partial<Pick<Shape, 'fill' | 'weight' | 'border' | 'borderColor' | 'hex' | 'borderHex'>>

/** The look fields to spread onto a shape: only those that are valid and not the default. */
export function lookOf(src: { fill?: unknown; weight?: unknown; border?: unknown; borderColor?: unknown; hex?: unknown; borderHex?: unknown }): ShapeLookFields {
  const fill = toFill(src.fill)
  const weight = toWeight(src.weight)
  const border = toBorder(src.border)
  const borderColor = toSwatch(src.borderColor)
  const hex = toHex(src.hex)
  const borderHex = toHex(src.borderHex)
  return {
    ...(fill && fill !== 'solid' ? { fill } : {}),
    ...(weight && weight !== 'medium' ? { weight } : {}),
    ...(border && border !== 'solid' ? { border } : {}),
    ...(borderColor ? { borderColor } : {}),
    ...(hex ? { hex } : {}),
    ...(borderHex ? { borderHex } : {}),
  }
}

export type ParsedCanvas = { ok: true; canvas: Canvas } | { ok: false }

/**
 * A canvas from its document. An empty document is an empty canvas (a canvas
 * just created). Anything that is not a canvas document is `ok: false`; inside
 * one, a shape or connector it cannot read is dropped.
 */
export function parseCanvas(raw: string): ParsedCanvas {
  if (!raw.trim()) return { ok: true, canvas: emptyCanvas() }
  let data: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { ok: false }
    data = parsed as Record<string, unknown>
  } catch {
    return { ok: false }
  }
  if (data.kind !== CANVAS_KIND) return { ok: false }
  const shapes: Shape[] = []
  for (const s of Array.isArray(data.shapes) ? data.shapes : []) {
    const o = s as Record<string, unknown>
    if (!o || typeof o.id !== 'string' || !KINDS.has(o.kind as ShapeKind)) continue
    const kind = o.kind as ShapeKind
    shapes.push({
      id: o.id, kind,
      x: num(o.x, 0), y: num(o.y, 0),
      w: Math.max(24, num(o.w, DEFAULT_SIZE[kind].w)), h: Math.max(24, num(o.h, DEFAULT_SIZE[kind].h)),
      text: typeof o.text === 'string' ? o.text : '',
      color: toSwatch(o.color) ?? 'yellow',
      ...(o.locked === true ? { locked: true } : {}),
      ...lookOf(o),
    })
  }
  const ids = new Set(shapes.map(s => s.id))
  const connectors = (Array.isArray(data.connectors) ? data.connectors : [])
    .map(c => c as Record<string, unknown>)
    .filter(c => c && typeof c.id === 'string' && ids.has(c.from as string) && ids.has(c.to as string))
    .map(c => {
      const conn: Connector = { id: c.id as string, from: c.from as string, to: c.to as string }
      if (CONN_SIDES.has(c.fromSide as ConnectorSide)) conn.fromSide = c.fromSide as ConnectorSide
      if (CONN_SIDES.has(c.toSide as ConnectorSide)) conn.toSide = c.toSide as ConnectorSide
      if (CONN_LINES.has(c.line as ConnectorLine)) conn.line = c.line as ConnectorLine
      const color = toSwatch(c.color)
      if (color) conn.color = color
      if (c.dashed === true) conn.dashed = true
      if (CONN_ARROWS.has(c.arrows as ConnectorArrows)) conn.arrows = c.arrows as ConnectorArrows
      const route = Array.isArray(c.route) ? (c.route as unknown[]).map(p => p as Record<string, unknown>) : []
      if (route.length && route.every(p => p && Number.isFinite(p.x) && Number.isFinite(p.y))) {
        conn.route = route.map(p => ({ x: p.x as number, y: p.y as number }))
      }
      return conn
    })
  return { ok: true, canvas: { kind: CANVAS_KIND, schema: CANVAS_SCHEMA, shapes, connectors } }
}

export const serializeCanvas = (c: Canvas) => JSON.stringify(c, null, 1)

export const newId = (prefix = 's') => `${prefix}${Date.now().toString(36).slice(-5)}${Math.random().toString(36).slice(2, 6)}`

/**
 * How much one wheel or pinch event zooms. A trackpad pinch sends many small
 * events and a mouse wheel few big ones, so the step is clamped: a notch of the
 * wheel changes the zoom by about a fifth, and a pinch follows the fingers gently.
 */
export function wheelZoomFactor(deltaY: number): number {
  return Math.exp(-Math.max(-40, Math.min(40, deltaY)) * 0.005)
}

/** How far one wheel event pans the canvas, in screen px, for a delta in the event's own units. */
export function wheelPanStep(delta: number, deltaMode: number): number {
  // A mouse wheel may report lines (mode 1) or pages (mode 2) rather than pixels.
  const px = deltaMode === 1 ? delta * 16 : deltaMode === 2 ? delta * 400 : delta
  return px * 1.6
}

// ---- geometry ----------------------------------------------------------------

export interface Pt { x: number; y: number }

export const center = (s: Shape): Pt => ({ x: s.x + s.w / 2, y: s.y + s.h / 2 })

// Connector routes (sides, elbows, curves) live in connectorGeometry.ts.

/** Height of a section's title tab, drawn above its box. */
export const SECTION_TITLE_H = 30

/**
 * How much a section's label is drawn larger than the canvas is, so it keeps its
 * on-screen size as you zoom out (as FigJam's do) and stays easy to grab. At 100%
 * and above it is the canvas's own size and zooms with it.
 */
export const labelScale = (zoom: number) => Math.max(1, 1 / zoom)

/** Sections draw first, under everything they hold. */
export const zOrdered = (shapes: Shape[]) => [...shapes.filter(s => s.kind === 'section'), ...shapes.filter(s => s.kind !== 'section')]

/**
 * The shape under *p*, topmost first. A section is picked by its title tab only,
 * as in FigJam: a press on its body is a press on the canvas (a marquee, a new
 * sticky), so it never gets in the way of the cards inside it. Locked shapes are
 * passed over, as presses go through them, unless *opts.locked* asks for them.
 * *opts.zoom* sizes the label's grab area to match its on-screen size.
 */
export function shapeAt(shapes: Shape[], p: Pt, opts: { locked?: boolean; zoom?: number } = {}): Shape | null {
  const k = labelScale(opts.zoom ?? 1)
  for (let i = shapes.length - 1; i >= 0; i--) {
    const s = shapes[i]
    if (s.locked && !opts.locked) continue
    if (s.kind === 'section') continue
    if (p.x >= s.x && p.x <= s.x + s.w && p.y >= s.y && p.y <= s.y + s.h) return s
  }
  for (let i = shapes.length - 1; i >= 0; i--) {
    const s = shapes[i]
    if (s.locked && !opts.locked) continue
    if (s.kind !== 'section') continue
    if (p.x >= s.x && p.x <= s.x + Math.min(s.w, 260 * k) && p.y >= s.y - SECTION_TITLE_H * k && p.y <= s.y) return s
  }
  return null
}

const centerIn = (s: Shape, box: Shape) => {
  const c = center(s)
  return c.x >= box.x && c.x <= box.x + box.w && c.y >= box.y && c.y <= box.y + box.h
}

/** What a section holds: every non-section shape whose centre is inside it. */
export const sectionMembers = (shapes: Shape[], sec: Shape) =>
  shapes.filter(s => s.kind !== 'section' && centerIn(s, sec))

export function boundsOf(shapes: Shape[]): { x: number; y: number; w: number; h: number } | null {
  if (!shapes.length) return null
  const x = Math.min(...shapes.map(s => s.x))
  const y = Math.min(...shapes.map(s => s.y))
  const r = Math.max(...shapes.map(s => s.x + s.w))
  const b = Math.max(...shapes.map(s => s.y + s.h))
  return { x, y, w: r - x, h: b - y }
}
