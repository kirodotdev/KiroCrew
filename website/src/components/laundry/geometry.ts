/**
 * Pure geometry for the laundry game (`./engine.ts`): hand-drawn outlines,
 * folding a polygon over a crease, resampling and point matching for morphs,
 * and the Kiro ghost outline read from the product's own mark.
 *
 * Everything here is deterministic given its `rand` source, so the engine's
 * tests can pin the folds and the egg with a seeded random.
 */
import ghostMarkSvg from '../../assets/kiro-ghost-mark.svg?raw'

export type Pt = number[]
export interface Layer { pts: Pt[]; lines: Pt[][] }
export interface Box { x0: number; y0: number; x1: number; y1: number }
/** A fold: `ax` 0 is a vertical crease at x = `at`, 1 a horizontal one at y = `at`.
 *  Points with `(p[ax] - at) * dir > 0` are the flap, which turns over the crease. */
export interface Fold { ax: 0 | 1; at: number; dir: 1 | -1 }
export type Rand = () => number

export const PI = Math.PI
export const clamp = (v: number, a: number, b: number) => (v < a ? a : v > b ? b : v)
export const lerp = (a: number, b: number, t: number) => a + (b - a) * t
export const easeOut = (t: number) => 1 - (1 - t) ** 3
export const easeIn = (t: number) => t * t * t
export const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2)
export const easeBack = (t: number) => 1 + 2.4 * (t - 1) ** 3 + 1.4 * (t - 1) ** 2

export function shuffle<T>(a: T[], rand: Rand): T[] {
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [a[i], a[j]] = [a[j], a[i]] }
  return a
}

/** Split each edge into ~`step`-long pieces with a small sideways wobble, so strokes look hand drawn. */
export function densify(poly: Pt[], step: number, closed: boolean, jit: number, rand: Rand): Pt[] {
  const out: Pt[] = [], n = poly.length, m = closed ? n : n - 1
  for (let i = 0; i < m; i++) {
    const a = poly[i], b = poly[(i + 1) % n]
    const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1
    const k = Math.max(1, Math.round(len / step))
    out.push([a[0], a[1]])
    for (let j = 1; j < k; j++) {
      const t = j / k, w = -jit + rand() * 2 * jit
      out.push([a[0] + dx * t - (dy / len) * w, a[1] + dy * t + (dx / len) * w])
    }
  }
  if (!closed) out.push(poly[n - 1].slice())
  return out
}

export function boxOf(list: Pt[][]): Box {
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9
  for (const pts of list) for (const p of pts) {
    if (p[0] < x0) x0 = p[0]
    if (p[0] > x1) x1 = p[0]
    if (p[1] < y0) y0 = p[1]
    if (p[1] > y1) y1 = p[1]
  }
  return { x0, y0, x1, y1 }
}

export function area(pts: Pt[]): number {
  let a = 0
  for (let i = 0, n = pts.length; i < n; i++) { const p = pts[i], q = pts[(i + 1) % n]; a += p[0] * q[1] - q[0] * p[1] }
  return Math.abs(a) / 2
}

const sd = (p: Pt, f: Fold) => (p[f.ax] - f.at) * f.dir

function clipPoly(poly: Pt[], f: Fold, flap: boolean): Pt[] {
  const out: Pt[] = []
  for (let i = 0, n = poly.length; i < n; i++) {
    const A = poly[i], B = poly[(i + 1) % n], da = sd(A, f), db = sd(B, f)
    const ia = flap ? da >= 0 : da <= 0, ib = flap ? db >= 0 : db <= 0
    if (ia) out.push(A)
    if (ia !== ib) {
      const t = da / (da - db)
      if (t > 0 && t < 1) out.push([A[0] + (B[0] - A[0]) * t, A[1] + (B[1] - A[1]) * t])
    }
  }
  return out
}

function clipLine(line: Pt[], f: Fold, flap: boolean): Pt[][] {
  const res: Pt[][] = []
  let cur: Pt[] = []
  for (let i = 0; i < line.length; i++) {
    const A = line[i], da = sd(A, f), ia = flap ? da >= 0 : da <= 0
    if (i > 0) {
      const P = line[i - 1], dp = sd(P, f), ip = flap ? dp >= 0 : dp <= 0
      if (ip !== ia && dp !== da) {
        const t = dp / (dp - da), X = [P[0] + (A[0] - P[0]) * t, P[1] + (A[1] - P[1]) * t]
        if (ia) cur = [X]
        else { cur.push(X); if (cur.length > 1) res.push(cur); cur = [] }
      }
    }
    if (ia) cur.push(A)
  }
  if (cur.length > 1) res.push(cur)
  return res
}

/** `c` = cos(angle): 1 = flat, 0 = standing on the crease, -1 = turned over (mirrored). */
function flipPt(p: Pt, f: Fold, c: number): Pt { const q = p.slice(); q[f.ax] = f.at + (p[f.ax] - f.at) * c; return q }

export function foldLayers(layers: Layer[], f: Fold, c: number): { stat: Layer[]; flap: Layer[] } {
  const stat: Layer[] = [], flap: Layer[] = []
  for (const L of layers) {
    const sp = clipPoly(L.pts, f, false)
    if (sp.length > 2 && area(sp) > 1) stat.push({ pts: sp, lines: L.lines.flatMap(l => clipLine(l, f, false)) })
    const fp = clipPoly(L.pts, f, true)
    if (fp.length > 2 && area(fp) > 1) {
      flap.push({ pts: fp.map(p => flipPt(p, f, c)), lines: L.lines.flatMap(l => clipLine(l, f, true)).map(l => l.map(p => flipPt(p, f, c))) })
    }
  }
  if (c < 0) flap.reverse()
  return { stat, flap }
}

export function creaseExtent(layers: Layer[], f: Fold): [number, number] {
  const o = 1 - f.ax
  let lo = 1e9, hi = -1e9
  for (const L of layers) {
    const P = L.pts
    for (let i = 0, n = P.length; i < n; i++) {
      const A = P[i], B = P[(i + 1) % n], da = sd(A, f), db = sd(B, f)
      if ((da <= 0 && db >= 0) || (da >= 0 && db <= 0)) {
        const t = da === db ? 0 : da / (da - db), v = A[o] + (B[o] - A[o]) * t
        if (v < lo) lo = v
        if (v > hi) hi = v
      }
    }
  }
  return [lo, hi]
}

export function resample(pts: Pt[], N: number): Pt[] {
  const n = pts.length, cum = [0]
  let L = 0
  for (let i = 0; i < n; i++) { const a = pts[i], b = pts[(i + 1) % n]; L += Math.hypot(b[0] - a[0], b[1] - a[1]); cum.push(L) }
  const out: Pt[] = []
  let j = 0
  for (let k = 0; k < N; k++) {
    const d = (L * k) / N
    while (j < n - 1 && cum[j + 1] < d) j++
    const a = pts[j], b = pts[(j + 1) % n], t = (d - cum[j]) / (cum[j + 1] - cum[j] || 1)
    out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
  }
  return out
}

/** Rotate the point order of `to` so each point lines up with its nearest partner in `from` (smooth morphs). */
export function align(from: Pt[], to: Pt[]): Pt[] {
  const N = from.length
  let best = 0, bd = Infinity
  for (let s = 0; s < N; s++) {
    let d = 0
    for (let i = 0; i < N; i += 2) { const p = from[i], q = to[(i + s) % N]; d += (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 }
    if (d < bd) { bd = d; best = s }
  }
  return to.map((_, i) => to[(i + best) % N])
}

// ---------- the Kiro ghost, read from the product's mark (src/assets/kiro-ghost-mark.svg) ----------

/** Points along one SVG path's `d`, for the absolute M/L/H/V/C/Z commands the mark uses. */
export function samplePath(d: string, perCurve = 16): Pt[] {
  const tokens = d.match(/[MLHVCZ]|-?\d*\.?\d+(?:e-?\d+)?/gi) || []
  const out: Pt[] = []
  let i = 0, cmd = '', x = 0, y = 0
  const num = () => Number(tokens[i++])
  while (i < tokens.length) {
    if (/^[A-Za-z]$/.test(tokens[i])) cmd = tokens[i++].toUpperCase()
    if (cmd === 'Z') continue
    if (cmd === 'M' || cmd === 'L') { x = num(); y = num(); out.push([x, y]) }
    else if (cmd === 'H') { x = num(); out.push([x, y]) }
    else if (cmd === 'V') { y = num(); out.push([x, y]) }
    else if (cmd === 'C') {
      const ax = num(), ay = num(), bx = num(), by = num(), ex = num(), ey = num()
      for (let k = 1; k <= perCurve; k++) {
        const t = k / perCurve, u = 1 - t
        out.push([u * u * u * x + 3 * u * u * t * ax + 3 * u * t * t * bx + t * t * t * ex,
          u * u * u * y + 3 * u * u * t * ay + 3 * u * t * t * by + t * t * t * ey])
      }
      x = ex; y = ey
    } else i++ // a command the mark does not use: skip its argument
  }
  return out
}

/** The mark's body outline and its two eyes, in a unit box (0..1 on each axis of the body). */
export interface GhostUnit { body: Pt[]; eyes: [number, number, number, number][]; aspect: number }

export function ghostFromMark(svg: string): GhostUnit {
  const paths = [...svg.matchAll(/<path\b[^>]*\sd="([^"]+)"/g)].map(m => samplePath(m[1]))
  const body = paths[0] || []
  const b = boxOf([body]), w = b.x1 - b.x0 || 1, h = b.y1 - b.y0 || 1
  const unit = (p: Pt): Pt => [(p[0] - b.x0) / w, (p[1] - b.y0) / h]
  const eyes = paths.slice(1, 3).map(e => {
    const eb = boxOf([e])
    return [((eb.x0 + eb.x1) / 2 - b.x0) / w, ((eb.y0 + eb.y1) / 2 - b.y0) / h, (eb.x1 - eb.x0) / 2 / w, (eb.y1 - eb.y0) / 2 / h] as [number, number, number, number]
  })
  return { body: body.map(unit), eyes, aspect: w / h }
}

export const GHOST_MARK = ghostFromMark(ghostMarkSvg)
export const GH = 101, GW = Math.round(GH * GHOST_MARK.aspect), NG = 96

/** The ghost centred on (cx, cy), `NG` points round its body, eyes as [x, y, rx, ry]. */
export function ghostShape(cx: number, cy: number): { pts: Pt[]; eyes: number[][] } {
  const P = (p: Pt): Pt => [cx + (p[0] - 0.5) * GW, cy + (p[1] - 0.5) * GH]
  return {
    pts: resample(GHOST_MARK.body.map(P), NG),
    eyes: GHOST_MARK.eyes.map(([ex, ey, rx, ry]) => [cx + (ex - 0.5) * GW, cy + (ey - 0.5) * GH, rx * GW, ry * GH]),
  }
}
