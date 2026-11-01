/**
 * The whiteboard's data (PoC): shapes, connectors bound to them, and the asks the
 * person pinned to shapes. Pure: parsing, the summary the conductor reads,
 * connector geometry, and the additions a conductor reply asks for.
 */

import { toHex } from './colorMath'

export const BOARD_ID_PREFIX = 'b0a4d'
export const isBoardId = (docId: string) => docId.startsWith(BOARD_ID_PREFIX)

/** A `section` is a FigJam section: a titled container whose cards it lays out. */
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
  /** Locked shapes cannot be picked, moved or edited from the board until unlocked. */
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
 * A line between two shapes. Everything past from/to is optional, so older boards
 * read unchanged: an unpinned side faces the other shape, the line is an elbow,
 * the colour is the board's ink, solid, with an arrow at the end.
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
export interface BoardAsk { id: string; shapeIds: string[] }

export interface Board {
  kind: 'kirocrew-board'
  version: 1
  shapes: Shape[]
  connectors: Connector[]
  asks: BoardAsk[]
}

/** The strong row of FigJam's colour picker, then its light row. */
export const STRONG_SWATCHES: Swatch[] = ['black', 'dark', 'red', 'orange', 'yellow', 'green', 'teal', 'blue', 'violet', 'magenta', 'white']
export const LIGHT_SWATCHES: Swatch[] = ['silver', 'smoke', 'rose', 'peach', 'cream', 'mint', 'ice', 'sky', 'lavender', 'blush']
export const SWATCHES: Swatch[] = [...STRONG_SWATCHES, ...LIGHT_SWATCHES]

/** A connector's line colour until one is picked: the board's ink. */
export const CONN_INK = 'color-mix(in srgb, var(--text) 68%, var(--bg))'

/** Names an earlier palette used, read as the nearest light colour. */
const LEGACY_SWATCH: Record<string, Swatch> = { pink: 'blush', purple: 'lavender', gray: 'smoke' }

/** A colour name from a file or a conductor's block, or undefined if it is not one. */
export function toSwatch(v: unknown): Swatch | undefined {
  if (typeof v !== 'string') return undefined
  if (SWATCHES.includes(v as Swatch)) return v as Swatch
  return LEGACY_SWATCH[v]
}

const FILLS = new Set<FillMode>(['solid', 'transparent', 'none'])
const WEIGHTS = new Set<StrokeWeight>(['thin', 'medium', 'thick'])
export const toFill = (v: unknown): FillMode | undefined => (FILLS.has(v as FillMode) ? (v as FillMode) : undefined)
const BORDERS = new Set<BorderStyle>(['solid', 'dashed', 'none'])
export const toBorder = (v: unknown): BorderStyle | undefined => (BORDERS.has(v as BorderStyle) ? (v as BorderStyle) : undefined)
export const toWeight = (v: unknown): StrokeWeight | undefined => (WEIGHTS.has(v as StrokeWeight) ? (v as StrokeWeight) : undefined)

/** A colour from a file or a conductor, and the fill it implies: an older "clear" was an outline, now black with no fill. */
export function colorAndFill(v: unknown): { color?: Swatch; fill?: FillMode } {
  if (v === 'clear') return { color: 'black', fill: 'none' }
  return { color: toSwatch(v) }
}

/**
 * Per swatch: a sticky's paper, a shape's fill and its border, and the ink that
 * reads on it. A strong colour fills with itself and borders in a deeper shade; a
 * light colour fills pale and borders in its strong sibling, as in FigJam.
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

export const emptyBoard = (): Board => ({ kind: 'kirocrew-board', version: 1, shapes: [], connectors: [], asks: [] })

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

/** A board from its file; anything unreadable is dropped, never thrown. */
export function parseBoard(raw: string): Board {
  if (!raw.trim()) return emptyBoard()
  let data: Record<string, unknown>
  try { data = JSON.parse(raw) as Record<string, unknown> } catch { return emptyBoard() }
  const shapes: Shape[] = []
  for (const s of Array.isArray(data.shapes) ? data.shapes : []) {
    const o = s as Record<string, unknown>
    if (typeof o.id !== 'string' || !KINDS.has(o.kind as ShapeKind)) continue
    const kind = o.kind as ShapeKind
    shapes.push({
      id: o.id, kind,
      x: num(o.x, 0), y: num(o.y, 0),
      w: Math.max(24, num(o.w, DEFAULT_SIZE[kind].w)), h: Math.max(24, num(o.h, DEFAULT_SIZE[kind].h)),
      text: typeof o.text === 'string' ? o.text : '',
      color: colorAndFill(o.color).color ?? 'yellow',
      ...(o.locked === true ? { locked: true } : {}),
      ...lookOf({ ...o, fill: o.fill ?? colorAndFill(o.color).fill }),
    })
  }
  const ids = new Set(shapes.map(s => s.id))
  const connectors = (Array.isArray(data.connectors) ? data.connectors : [])
    .map(c => c as Record<string, unknown>)
    .filter(c => typeof c.id === 'string' && ids.has(c.from as string) && ids.has(c.to as string))
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
  const asks = (Array.isArray(data.asks) ? data.asks : [])
    .map(a => a as Record<string, unknown>)
    .filter(a => typeof a.id === 'string' && Array.isArray(a.shapeIds))
    .map(a => ({ id: a.id as string, shapeIds: (a.shapeIds as unknown[]).filter((x): x is string => typeof x === 'string') }))
  return { kind: 'kirocrew-board', version: 1, shapes, connectors, asks }
}

export const serializeBoard = (b: Board) => JSON.stringify(b, null, 1)

export const newId = (prefix = 's') => `${prefix}${Date.now().toString(36).slice(-5)}${Math.random().toString(36).slice(2, 6)}`

/**
 * How much one wheel or pinch event zooms. A trackpad pinch sends many small
 * events and a mouse wheel few big ones, so the step is clamped: a notch of the
 * wheel changes the zoom by about a fifth, and a pinch follows the fingers gently.
 */
export function wheelZoomFactor(deltaY: number): number {
  return Math.exp(-Math.max(-40, Math.min(40, deltaY)) * 0.005)
}

/** How far one wheel event pans the board, in screen px, for a delta in the event's own units. */
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

/** Sections draw first, under everything they hold. */
export const zOrdered = (shapes: Shape[]) => [...shapes.filter(s => s.kind === 'section'), ...shapes.filter(s => s.kind !== 'section')]

/**
 * The shape under *p*, topmost first. A section is picked by its title tab only,
 * as in FigJam: a press on its body is a press on the board (a marquee, a new
 * sticky), so it never gets in the way of the cards inside it. Locked shapes are
 * passed over, as presses go through them, unless *opts.locked* asks for them.
 */
export function shapeAt(shapes: Shape[], p: Pt, opts: { locked?: boolean } = {}): Shape | null {
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
    if (p.x >= s.x && p.x <= s.x + Math.min(s.w, 260) && p.y >= s.y - SECTION_TITLE_H && p.y <= s.y) return s
  }
  return null
}

// ---- sections: containers that lay out their cards ------------------------------------

const SECTION_PAD = 20
const CARD_GAP = 12
const CARD_MIN_W = 200
const SECTION_MIN_H = 160

const centerIn = (s: Shape, box: Shape) => {
  const c = center(s)
  return c.x >= box.x && c.x <= box.x + box.w && c.y >= box.y && c.y <= box.y + box.h
}

/** What a section holds: every non-section shape whose centre is inside it. */
export const sectionMembers = (shapes: Shape[], sec: Shape) =>
  shapes.filter(s => s.kind !== 'section' && centerIn(s, sec))

/** The sections a shape sits in. */
export const sectionsOf = (shapes: Shape[], s: Shape) =>
  shapes.filter(x => x.kind === 'section' && x.id !== s.id && centerIn(s, x))

/** A card's height for its text at width *w*: as tall as it needs, no taller. */
export function fitHeight(kind: ShapeKind, text: string, w: number): number {
  const font = kind === 'text' ? 20 : kind === 'sticky' ? 16 : 15
  const pad = kind === 'text' ? 4 : kind === 'sticky' ? 16 : 14
  const perLine = Math.max(6, Math.floor((w - pad * 2) / (font * 0.56)))
  const lines = (text || ' ').split('\n').reduce((n, p) => n + Math.max(1, Math.ceil(p.length / perLine)), 0)
  const h = Math.ceil(pad * 2 + lines * font * 1.35 + 4)
  return kind === 'text' ? h : Math.max(72, h)
}

/**
 * The board with section *secId*'s cards laid out: evenly spaced in as many
 * columns as its width holds, each card the column's width and as tall as its
 * text, in reading order, then *append* in the order given. The section grows
 * or shrinks to fit. This is what keeps a conductor's board tidy: it says which
 * section a card belongs in, never where to draw it.
 */
export function reflowSection(shapes: Shape[], secId: string, append: string[] = []): Shape[] {
  const sec = shapes.find(s => s.id === secId && s.kind === 'section')
  if (!sec) return shapes
  const tail = new Set(append)
  const members = [
    ...sectionMembers(shapes, sec).filter(s => !tail.has(s.id)).sort((a, b) => (a.y - b.y) || (a.x - b.x)),
    ...append.map(id => shapes.find(s => s.id === id && s.kind !== 'section')).filter((s): s is Shape => !!s),
  ]
  if (!members.length) return shapes
  const inner = sec.w - SECTION_PAD * 2
  const cols = Math.max(1, Math.floor((inner + CARD_GAP) / (CARD_MIN_W + CARD_GAP)))
  const cw = Math.floor((inner - (cols - 1) * CARD_GAP) / cols)
  const place = new Map<string, Pick<Shape, 'x' | 'y' | 'w' | 'h'>>()
  let y = sec.y + SECTION_PAD
  for (let i = 0; i < members.length; i += cols) {
    const row = members.slice(i, i + cols)
    const hs = row.map(m => fitHeight(m.kind, m.text, cw))
    row.forEach((m, j) => place.set(m.id, { x: sec.x + SECTION_PAD + j * (cw + CARD_GAP), y, w: cw, h: hs[j] }))
    y += Math.max(...hs) + CARD_GAP
  }
  const h = Math.max(SECTION_MIN_H, y - CARD_GAP + SECTION_PAD - sec.y)
  return shapes.map(s => (s.id === sec.id ? { ...s, h } : place.has(s.id) ? { ...s, ...place.get(s.id)! } : s))
}

export function boundsOf(shapes: Shape[]): { x: number; y: number; w: number; h: number } | null {
  if (!shapes.length) return null
  const x = Math.min(...shapes.map(s => s.x))
  const y = Math.min(...shapes.map(s => s.y))
  const r = Math.max(...shapes.map(s => s.x + s.w))
  const b = Math.max(...shapes.map(s => s.y + s.h))
  return { x, y, w: r - x, h: b - y }
}

// ---- what the conductor reads and adds ------------------------------------------------

/** The board as the conductor reads it: one line per shape, then the connections. */
export function summarizeBoard(b: Board): string {
  if (!b.shapes.length) return '(the board is empty)'
  const lines = b.shapes.map(s => {
    const text = s.text.replace(/\s+/g, ' ').trim()
    const holds = s.kind === 'section' ? ` | contains: ${sectionMembers(b.shapes, s).map(m => m.id).join(', ') || 'nothing'}` : ''
    return `- ${s.id} | ${s.kind} | ${s.color} | at (${Math.round(s.x)}, ${Math.round(s.y)}) size ${Math.round(s.w)}x${Math.round(s.h)} | ${text ? JSON.stringify(text) : '(no text)'}${holds}${s.locked ? ' | locked' : ''}`
  })
  const conns = b.connectors.map(c => `- ${c.from} -> ${c.to}`)
  return [`Shapes:`, ...lines, conns.length ? 'Connections:' : 'Connections: none', ...conns].join('\n')
}

export interface BoardAdd {
  shapes?: { ref?: string; kind?: ShapeKind; text?: string; color?: Swatch; fill?: FillMode; weight?: StrokeWeight; border?: BorderStyle; borderColor?: Swatch; hex?: string; borderHex?: string; in?: string; near?: string; side?: 'below' | 'right'; x?: number; y?: number; w?: number; h?: number }[]
  connectors?: { from: string; to: string }[]
}

const GAP = 24

const overlaps = (a: { x: number; y: number; w: number; h: number }, b: Shape) =>
  a.x < b.x + b.w + 8 && a.x + a.w + 8 > b.x && a.y < b.y + b.h + 8 && a.y + a.h + 8 > b.y

/** Step *box* along its direction until it covers no shape: new cards never land on others. */
function clearSpot(box: { x: number; y: number; w: number; h: number }, shapes: Shape[], side: 'below' | 'right') {
  const at = { ...box }
  for (let i = 0; i < 400 && shapes.some(s => overlaps(at, s)); i++) {
    if (side === 'below') at.y += 16
    else at.x += 16
  }
  return at
}

/**
 * The board with a conductor's additions: new shapes and connectors only, never a
 * change to what is there. A shape whose text already sits on the board is not
 * added twice. `ref` lets a connector name a shape added in the same block.
 */
export function applyBoardAdd(b: Board, add: BoardAdd): Board {
  const shapes = [...b.shapes]
  const connectors = [...b.connectors]
  const refs = new Map<string, string>()
  const texts = new Set(shapes.map(s => s.text.trim()).filter(Boolean))
  // Additions without a neighbour line up to the right of everything.
  const all = boundsOf(shapes)
  let freeX = all ? all.x + all.w + 80 : 0
  const freeY = all ? all.y : 0
  const stacked = new Map<string, number>()
  // Cards headed for a section, by section, in the order given: laid out at the end.
  const into = new Map<string, string[]>()
  for (const s of add.shapes ?? []) {
    const kind: ShapeKind = s.kind && KINDS.has(s.kind) ? s.kind : 'sticky'
    const text = (s.text ?? '').trim()
    if (text && texts.has(text)) {
      const existing = shapes.find(x => x.text.trim() === text)
      if (s.ref && existing) refs.set(s.ref, existing.id)
      continue
    }
    const size = {
      w: Math.max(24, num(s.w, DEFAULT_SIZE[kind].w)),
      h: Math.max(24, num(s.h, DEFAULT_SIZE[kind].h)),
    }
    const near = s.near ? shapes.find(x => x.id === (refs.get(s.near!) ?? s.near)) : undefined
    const home = s.in && kind !== 'section' ? shapes.find(x => x.kind === 'section' && x.id === (refs.get(s.in!) ?? s.in)) : undefined
    let x: number
    let y: number
    if (home) { x = home.x; y = home.y }
    else if (typeof s.x === 'number' && typeof s.y === 'number') { x = s.x; y = s.y }
    else if (near) {
      // Below by default: a column of cards under its header. Each next card
      // starts under the last one, then steps past anything in the way.
      const side = s.side === 'right' ? 'right' : 'below'
      const n = stacked.get(`${near.id}:${side}`) ?? 0
      stacked.set(`${near.id}:${side}`, n + 1)
      const start = side === 'below'
        ? { x: near.x + (near.w - size.w) / 2, y: near.y + near.h + GAP, w: size.w, h: size.h }
        : { x: near.x + near.w + GAP * 2, y: near.y, w: size.w, h: size.h }
      const spot = clearSpot(start, shapes, side)
      x = spot.x
      y = spot.y
    } else {
      const spot = clearSpot({ x: freeX, y: freeY, w: size.w, h: size.h }, shapes, 'right')
      x = spot.x
      y = spot.y
      freeX = x + size.w + 40
    }
    const shape: Shape = {
      id: newId(), kind, x, y, w: size.w, h: size.h, text,
      color: colorAndFill(s.color).color ?? 'yellow',
      ...lookOf({ ...s, fill: s.fill ?? colorAndFill(s.color).fill }),
    }
    shapes.push(shape)
    if (text) texts.add(text)
    if (s.ref) refs.set(s.ref, shape.id)
    if (home) into.set(home.id, [...(into.get(home.id) ?? []), shape.id])
  }
  let laid = shapes
  for (const [sec, ids] of into) laid = reflowSection(laid, sec, ids)
  shapes.splice(0, shapes.length, ...laid)
  const ids = new Set(shapes.map(s => s.id))
  for (const c of add.connectors ?? []) {
    const from = refs.get(c.from) ?? c.from
    const to = refs.get(c.to) ?? c.to
    if (!ids.has(from) || !ids.has(to) || from === to) continue
    if (connectors.some(k => k.from === from && k.to === to)) continue
    connectors.push({ id: newId('c'), from, to })
  }
  return { ...b, shapes, connectors }
}

const ADD_RE = /```board-add[^\n]*\n([\s\S]*?)\n```[ \t]*(?=\n|$)/g

/** The `board-add` blocks of a conductor reply, parsed; unreadable ones are skipped. */
export function boardAddsIn(text: string): BoardAdd[] {
  const out: BoardAdd[] = []
  for (const m of text.matchAll(ADD_RE)) {
    try { out.push(JSON.parse(m[1]) as BoardAdd) } catch { /* not JSON: nothing to add */ }
  }
  return out
}

/** Changes to what is on the board: shapes by id, partial; deletions by id. */
export interface BoardEdit {
  /** `in` moves a card into that section: it joins the end, and both columns re-flow. */
  shapes?: ({ id: string; in?: string } & Partial<Pick<Shape, 'x' | 'y' | 'w' | 'h' | 'text' | 'color' | 'kind' | 'fill' | 'weight' | 'border' | 'borderColor' | 'hex' | 'borderHex'>>)[]
  delete?: string[]
  /** Sections to lay out again, as they stand. */
  arrange?: string[]
  /** Connectors to remove: by id, or by the two shapes they join. */
  deleteConnectors?: (string | { from: string; to: string })[]
}

/** A conductor's change to a shape's look; unnamed or invalid values leave the shape's own. */
function editedLook(s: Shape, c: { color?: unknown; fill?: unknown; weight?: unknown; border?: unknown; borderColor?: unknown; hex?: unknown; borderHex?: unknown }): ShapeLookFields {
  const look = lookOf({
    fill: toFill(c.fill) ?? colorAndFill(c.color).fill ?? s.fill,
    weight: toWeight(c.weight) ?? s.weight,
    border: toBorder(c.border) ?? s.border,
    borderColor: toSwatch(c.borderColor) ?? s.borderColor,
    // Naming a swatch without a hex swaps a custom colour out; naming a hex sets it.
    hex: toHex(c.hex) ?? (toSwatch(c.color) ? undefined : s.hex),
    borderHex: toHex(c.borderHex) ?? (toSwatch(c.borderColor) ? undefined : s.borderHex),
  })
  return { fill: look.fill, weight: look.weight, border: look.border, borderColor: look.borderColor, hex: look.hex, borderHex: look.borderHex }
}

/**
 * The board with a conductor's edits. An id it does not know is skipped, so a
 * stale edit cannot land on the wrong shape; a value of the wrong type is
 * ignored rather than written. Deleting a shape takes its connectors with it.
 */
export function applyBoardEdit(b: Board, edit: BoardEdit): Board {
  const changes = new Map((edit.shapes ?? []).filter(s => s && typeof s.id === 'string').map(s => [s.id, s]))
  let shapes = b.shapes.map(s => {
    const c = changes.get(s.id)
    if (!c) return s
    const kind = c.kind && KINDS.has(c.kind) ? c.kind : s.kind
    return {
      ...s, kind,
      x: num(c.x, s.x), y: num(c.y, s.y),
      w: Math.max(24, num(c.w, s.w)), h: Math.max(24, num(c.h, s.h)),
      text: typeof c.text === 'string' ? c.text : s.text,
      color: colorAndFill(c.color).color ?? s.color,
      ...editedLook(s, c),
    }
  })
  const gone = new Set((edit.delete ?? []).filter((id): id is string => typeof id === 'string'))
  shapes = shapes.filter(s => !gone.has(s.id))
  const connGone = edit.deleteConnectors ?? []
  const connectors = b.connectors.filter(c => !gone.has(c.from) && !gone.has(c.to) && !connGone.some(d =>
    typeof d === 'string' ? d === c.id : d && d.from === c.from && d.to === c.to))
  // Cards moving between sections, then sections to tidy. A section that loses or
  // gains a card, or whose card changed text, is laid out again.
  const touched = new Set<string>()
  for (const c of edit.shapes ?? []) {
    if (!c || typeof c.id !== 'string') continue
    const card = shapes.find(s => s.id === c.id)
    if (!card || card.kind === 'section') continue
    const was = sectionsOf(shapes, card)
    if (typeof c.in === 'string' && shapes.some(s => s.id === c.in && s.kind === 'section') && !was.some(s => s.id === c.in)) {
      shapes = reflowSection(shapes, c.in, [card.id])
      was.forEach(s => touched.add(s.id))
    } else if (typeof c.text === 'string') was.forEach(s => touched.add(s.id))
  }
  for (const id of edit.arrange ?? []) if (typeof id === 'string') touched.add(id)
  for (const id of touched) shapes = reflowSection(shapes, id)
  const asks = b.asks.filter(a => a.shapeIds.some(id => !gone.has(id)))
  return { ...b, shapes, connectors, asks }
}

const EDIT_RE = /```board-edit[^\n]*\n([\s\S]*?)\n```[ \t]*(?=\n|$)/g

/** The `board-edit` blocks of a conductor reply, parsed; unreadable ones are skipped. */
export function boardEditsIn(text: string): BoardEdit[] {
  const out: BoardEdit[] = []
  for (const m of text.matchAll(EDIT_RE)) {
    try { out.push(JSON.parse(m[1]) as BoardEdit) } catch { /* not JSON: nothing to change */ }
  }
  return out
}
