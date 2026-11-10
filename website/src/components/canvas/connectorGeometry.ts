/**
 * Connector geometry for the canvas, after FigJam: each end attaches to
 * the midpoint of one side of its shape, and the line between them is an elbow
 * with rounded corners (the default), a straight line, or a curve. Pure.
 */

import type { Connector, ConnectorLine, ConnectorSide, Pt, Shape } from './canvasModel'

export type Side = ConnectorSide
export const SIDES: Side[] = ['top', 'right', 'bottom', 'left']

const NORMAL: Record<Side, Pt> = { top: { x: 0, y: -1 }, right: { x: 1, y: 0 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 } }

/** A box: a shape, or a bare point while a connector end is being dragged. */
type Box = Pick<Shape, 'x' | 'y' | 'w' | 'h'>

/** The midpoint of one side; for ellipses and diamonds it is on the outline too. */
export function sideAnchor(s: Box, side: Side): Pt {
  if (side === 'top') return { x: s.x + s.w / 2, y: s.y }
  if (side === 'bottom') return { x: s.x + s.w / 2, y: s.y + s.h }
  if (side === 'left') return { x: s.x, y: s.y + s.h / 2 }
  return { x: s.x + s.w, y: s.y + s.h / 2 }
}

const mid = (s: Box): Pt => ({ x: s.x + s.w / 2, y: s.y + s.h / 2 })

/** The side of *s* that faces *p*: across the wider gap, as FigJam picks it. */
export function facingSide(s: Box, p: Pt): Side {
  const c = mid(s)
  const dx = p.x - c.x
  const dy = p.y - c.y
  // Normalised by the box, so a wide shape's side faces what is beside it.
  if (Math.abs(dx) / Math.max(1, s.w) >= Math.abs(dy) / Math.max(1, s.h)) return dx >= 0 ? 'right' : 'left'
  return dy >= 0 ? 'bottom' : 'top'
}

/** The side whose midpoint is within *reach* of *p*, if any. */
export function sideNear(s: Box, p: Pt, reach: number): Side | null {
  let best: Side | null = null
  let bestD = reach
  for (const side of SIDES) {
    const a = sideAnchor(s, side)
    const d = Math.hypot(a.x - p.x, a.y - p.y)
    if (d <= bestD) { best = side; bestD = d }
  }
  return best
}

/** The two sides a connector uses: the pinned ones, or the pair that face each other. */
export function connectorSides(from: Box, to: Box, fromSide?: Side, toSide?: Side): [Side, Side] {
  const fs = fromSide ?? facingSide(from, toSide ? sideAnchor(to, toSide) : mid(to))
  const ts = toSide ?? facingSide(to, sideAnchor(from, fs))
  return [fs, ts]
}

export type LineType = ConnectorLine

export interface Route {
  /** SVG path data, in canvas units. */
  d: string
  a: Pt
  b: Pt
  /** The route's corner points, for its bounding box. */
  points: Pt[]
}

/** How far an elbow runs straight out of a side before it may turn. */
const STUB = 24
/** Corner radius of an elbow, as on FigJam's connectors. */
const RADIUS = 16

const add = (p: Pt, n: Pt, k: number): Pt => ({ x: p.x + n.x * k, y: p.y + n.y * k })
const horizontal = (side: Side) => side === 'left' || side === 'right'

/** The elbow's corners from *a* (leaving along *na*) to *b* (entered against *nb*). */
function elbowPoints(a: Pt, sa: Side, b: Pt, sb: Side): Pt[] {
  const na = NORMAL[sa]
  const nb = NORMAL[sb]
  const p1 = add(a, na, STUB)
  const p2 = add(b, nb, STUB)
  let middle: Pt[]
  if (horizontal(sa) === horizontal(sb)) {
    const h = horizontal(sa)
    const along = (p: Pt) => (h ? p.x : p.y)
    const across = (p: Pt) => (h ? p.y : p.x)
    const pt = (u: number, v: number): Pt => (h ? { x: u, y: v } : { x: v, y: u })
    const n = h ? na.x : na.y
    if (sa === sb) {
      // Both ends on the same side: out past the further one and back.
      const u = n > 0 ? Math.max(along(p1), along(p2)) : Math.min(along(p1), along(p2))
      middle = [pt(u, across(p1)), pt(u, across(p2))]
    } else if ((along(p2) - along(p1)) * n >= 0) {
      // Facing each other with room between: turn halfway.
      const u = (along(p1) + along(p2)) / 2
      middle = [pt(u, across(p1)), pt(u, across(p2))]
    } else {
      // Back to back: go round, halfway across.
      const v = (across(p1) + across(p2)) / 2
      middle = [pt(along(p1), v), pt(along(p2), v)]
    }
  } else {
    // One horizontal end, one vertical: a single corner where they meet, if the
    // last leg then enters the target the right way; otherwise one more turn.
    const corner = horizontal(sa) ? { x: p2.x, y: p1.y } : { x: p1.x, y: p2.y }
    const enters = horizontal(sa) ? (p2.y - corner.y) * nb.y <= 0 : (p2.x - corner.x) * nb.x <= 0
    const leaves = horizontal(sa) ? (corner.x - p1.x) * na.x >= 0 : (corner.y - p1.y) * na.y >= 0
    middle = enters && leaves ? [corner] : horizontal(sa) ? [{ x: p1.x, y: p2.y }] : [{ x: p2.x, y: p1.y }]
  }
  return simplify([a, p1, ...middle, p2, b])
}

/** Drops repeated and collinear points, so each corner is a real turn. */
function simplify(pts: Pt[]): Pt[] {
  const out: Pt[] = []
  for (const p of pts) {
    const last = out[out.length - 1]
    if (last && Math.abs(last.x - p.x) < 0.01 && Math.abs(last.y - p.y) < 0.01) continue
    if (out.length >= 2) {
      const q = out[out.length - 2]
      const cross = (last.x - q.x) * (p.y - q.y) - (last.y - q.y) * (p.x - q.x)
      if (Math.abs(cross) < 0.01) { out[out.length - 1] = p; continue }
    }
    out.push(p)
  }
  return out
}

/** A polyline with each corner rounded off, never by more than half a leg. */
function roundedPath(pts: Pt[]): string {
  let d = `M${pts[0].x},${pts[0].y}`
  for (let i = 1; i < pts.length - 1; i++) {
    const p = pts[i]
    const prev = pts[i - 1]
    const next = pts[i + 1]
    const lin = Math.hypot(p.x - prev.x, p.y - prev.y)
    const lout = Math.hypot(next.x - p.x, next.y - p.y)
    const r = Math.min(RADIUS, lin / 2, lout / 2)
    const s = add(p, { x: (prev.x - p.x) / lin, y: (prev.y - p.y) / lin }, r)
    const e = add(p, { x: (next.x - p.x) / lout, y: (next.y - p.y) / lout }, r)
    d += ` L${s.x},${s.y} Q${p.x},${p.y} ${e.x},${e.y}`
  }
  const last = pts[pts.length - 1]
  return `${d} L${last.x},${last.y}`
}

/**
 * An elbow someone has shaped by hand: its stored corners, with the first and
 * last pulled back in line with their ends, so the route still meets both shapes
 * squarely after either one moves.
 */
function shapedPoints(a: Pt, sa: Side, b: Pt, sb: Side, route: Pt[]): Pt[] {
  const pts = route.map(p => ({ ...p }))
  const first = pts[0]
  const last = pts[pts.length - 1]
  if (horizontal(sa)) first.y = a.y
  else first.x = a.x
  if (horizontal(sb)) last.y = b.y
  else last.x = b.x
  return simplify([a, ...pts, b])
}

/** The two ends of a segment, and the axis it moves along when dragged. */
export interface SegmentDrag { points: Pt[]; seg: number; axis: 'x' | 'y' }

/**
 * Ready a drag of segment *seg* of an elbow's *points*. A segment that touches an
 * end cannot move without tearing off its shape, so as in FigJam a short stub is
 * kept on that end and the segment past it moves instead, adding a jog.
 */
export function segmentDrag(points: Pt[], seg: number, sa: Side, sb: Side): SegmentDrag {
  let pts = points.map(p => ({ ...p }))
  let i = seg
  if (i === 0) {
    const s = add(pts[0], NORMAL[sa], STUB)
    pts = [pts[0], s, { ...s }, ...pts.slice(1)]
    i = 2
  }
  if (i + 1 === pts.length - 1) {
    const end = pts[pts.length - 1]
    const s = add(end, NORMAL[sb], STUB)
    pts = [...pts.slice(0, -1), { ...s }, s, end]
  }
  const p = pts[i]
  const q = pts[i + 1]
  return { points: pts, seg: i, axis: Math.abs(p.y - q.y) < 0.01 ? 'y' : 'x' }
}

/** The stored corners after moving the dragged segment to *value* on its axis. */
export function movedSegment(d: SegmentDrag, value: number): Pt[] {
  const pts = d.points.map((p, k) => (k === d.seg || k === d.seg + 1 ? { ...p, [d.axis]: value } : p))
  return pts.slice(1, -1)
}

/** The route of a connector between two boxes, on its sides, in its line type. */
export function connectorRoute(from: Box, to: Box, c: Pick<Connector, 'fromSide' | 'toSide' | 'line' | 'route' | 'arrows'>): Route & { sides: [Side, Side] } {
  const [sa, sb] = connectorSides(from, to, c.fromSide, c.toSide)
  return { ...routeOn(from, to, sa, sb, c), sides: [sa, sb] }
}

/**
 * Space left between an arrowhead's tip and the shape it points at, in canvas
 * units. The drawn line stops this short; the route's ends (`a`, `b`) stay on the
 * shapes' sides, where the handles sit.
 */
const ARROW_GAP = 9

/** *p* moved *by* toward *toward*, never past the halfway point. */
function pulled(p: Pt, toward: Pt, by: number): Pt {
  const len = Math.hypot(toward.x - p.x, toward.y - p.y)
  if (len < 0.01) return p
  const k = Math.min(by, len / 2) / len
  return { x: p.x + (toward.x - p.x) * k, y: p.y + (toward.y - p.y) * k }
}

/** The corners as drawn: the ends pulled in by the gap where an arrowhead sits. */
function trimmed(pts: Pt[], startGap: number, endGap: number): Pt[] {
  const out = pts.map(p => ({ ...p }))
  const n = out.length
  if (startGap) out[0] = pulled(pts[0], pts[1], startGap)
  if (endGap) out[n - 1] = pulled(pts[n - 1], pts[n - 2], endGap)
  return out
}

function routeOn(from: Box, to: Box, sa: Side, sb: Side, c: Pick<Connector, 'line' | 'route' | 'arrows'>): Route {
  const a = sideAnchor(from, sa)
  const b = sideAnchor(to, sb)
  const line = c.line ?? 'elbow'
  const arrows = c.arrows ?? 'end'
  const endGap = arrows === 'none' ? 0 : ARROW_GAP
  const startGap = arrows === 'both' ? ARROW_GAP : 0
  if (line === 'straight') {
    const [s, e] = trimmed([a, b], startGap, endGap)
    return { d: `M${s.x},${s.y} L${e.x},${e.y}`, a, b, points: [a, b] }
  }
  if (line === 'curve') {
    const k = Math.max(40, Math.hypot(b.x - a.x, b.y - a.y) / 2.5)
    const c1 = add(a, NORMAL[sa], k)
    const c2 = add(b, NORMAL[sb], k)
    // The curve arrives along the side's normal, so the gap is taken along it.
    const s = add(a, NORMAL[sa], startGap)
    const e = add(b, NORMAL[sb], endGap)
    return { d: `M${s.x},${s.y} C${c1.x},${c1.y} ${c2.x},${c2.y} ${e.x},${e.y}`, a, b, points: [a, c1, c2, b] }
  }
  const points = c.route?.length ? shapedPoints(a, sa, b, sb, c.route) : elbowPoints(a, sa, b, sb)
  return { d: roundedPath(trimmed(points, startGap, endGap)), a, b, points }
}

/** A bare point as a box, for a connector end that follows the pointer. */
export const pointBox = (p: Pt): Box => ({ x: p.x, y: p.y, w: 0, h: 0 })
