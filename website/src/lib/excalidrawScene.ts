/**
 * Read-only renderer for Excalidraw scenes (`.excalidraw` JSON).
 *
 * Draws through **rough.js**, which is the same engine Excalidraw itself uses,
 * so the hand-drawn look is reproduced by the real sketching code rather than
 * approximated. Preserving each element's `seed` is what keeps a diagram stable
 * across re-renders — rough.js derives its jitter from the seed, so dropping it
 * would make every repaint wobble.
 *
 * Why not `@excalidraw/excalidraw`: it unpacks to ~46.8 MB and expects
 * `window.EXCALIDRAW_ASSET_PATH` to point at a CDN to fetch fonts at runtime.
 * Both are disqualifying here — the dashboard must not need the network to draw
 * a diagram, and `MermaidBlock` already treats 90-130 KB gzip as heavy enough to
 * require a dynamic `import()`. rough.js is 169 KB unpacked and is ALREADY in
 * the production tree as a transitive dependency of mermaid, so this feature
 * adds no new package to the install.
 *
 * This is a viewer, not the editor: faithful, not pixel-exact. Bound-text
 * layout inside containers and arrow re-routing are approximated, and
 * `embeddable`/`iframe` elements are skipped.
 *
 * SECURITY — scene JSON is untrusted (model-generated, or arbitrary file
 * content the user opened). Two inputs are attacker-controlled and are
 * validated rather than trusted:
 *   - `files[].dataURL` lands in an `<image href>`, so it is restricted to a
 *     raster `data:image/*;base64` allowlist (`safeImageDataUrl`).
 *   - colors land in presentation attributes, so they are pattern-checked
 *     (`safeColor`).
 * The SVG is built with `createElementNS`/`setAttribute` throughout — never
 * string concatenation — so there is no markup-injection surface to begin with.
 */
import rough from 'roughjs'
import type { RoughSVG } from 'roughjs/bin/svg'
import type { Options as RoughOptions } from 'roughjs/bin/core'

const SVG_NS = 'http://www.w3.org/2000/svg'

/* ── scene shape ──────────────────────────────────────────────────────────
 * Deliberately a permissive subset. Every field is optional because scenes are
 * written by many different Excalidraw versions and a missing field must
 * degrade to a default instead of throwing. */

export interface ExcalidrawElement {
  type?: string
  x?: number
  y?: number
  width?: number
  height?: number
  angle?: number
  strokeColor?: string
  backgroundColor?: string
  fillStyle?: string
  strokeWidth?: number
  strokeStyle?: string
  roughness?: number
  opacity?: number
  seed?: number
  roundness?: { type?: number; value?: number } | null
  isDeleted?: boolean
  points?: number[][]
  startArrowhead?: string | null
  endArrowhead?: string | null
  text?: string
  fontSize?: number
  fontFamily?: number
  textAlign?: string
  lineHeight?: number
  containerId?: string | null
  fileId?: string
  name?: string | null
}

export interface ExcalidrawFile {
  dataURL?: string
  mimeType?: string
}

export interface ExcalidrawScene {
  type?: string
  version?: number
  elements: ExcalidrawElement[]
  appState?: { viewBackgroundColor?: string | null }
  files?: Record<string, ExcalidrawFile> | null
}

/** Element types this renderer knows how to draw. Anything else is skipped so a
 *  scene from a newer Excalidraw degrades gracefully instead of failing. */
const DRAWABLE = new Set([
  'rectangle', 'diamond', 'ellipse', 'line', 'arrow',
  'freedraw', 'text', 'image', 'frame', 'magicframe',
])

/* ── input validation ─────────────────────────────────────────────────── */

/** Hex, rgb()/rgba(), hsl()/hsla(), or a bare CSS keyword. Deliberately strict:
 *  anything with parens beyond the listed functions, a semicolon, or a `url(`
 *  is rejected so a color can never smuggle syntax into an attribute. */
const SAFE_COLOR_RE = /^(#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%deg]+\)|[a-z]{3,20})$/i

export function safeColor(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback
  const v = value.trim()
  if (v === 'transparent') return 'transparent'
  return SAFE_COLOR_RE.test(v) ? v : fallback
}

/**
 * Allow only raster image data URLs.
 *
 * `svg+xml` is deliberately EXCLUDED. Browsers do not run scripts for an SVG
 * loaded as an image, but an SVG data URL is still a parser surface we gain
 * nothing from accepting, and the failure mode if that assumption ever breaks
 * is script execution with the dashboard's origin. Raster only.
 */
const SAFE_IMAGE_DATA_URL_RE = /^data:image\/(png|jpeg|jpg|gif|webp|bmp|avif);base64,[a-z0-9+/=\s]+$/i

export function safeImageDataUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  return SAFE_IMAGE_DATA_URL_RE.test(value.trim()) ? value.trim() : null
}

/* ── parsing ──────────────────────────────────────────────────────────── */

/**
 * Parse scene JSON, tolerantly.
 *
 * Returns null for anything unusable so callers can fall back to showing the
 * raw source. A scene with an empty `elements` array IS valid (an empty canvas)
 * and parses successfully — only malformed JSON or a missing/non-array
 * `elements` is a failure.
 */
export function parseExcalidrawScene(raw: string): ExcalidrawScene | null {
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return null
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const obj = data as Record<string, unknown>
  if (!Array.isArray(obj.elements)) return null
  const files = obj.files && typeof obj.files === 'object' && !Array.isArray(obj.files)
    ? (obj.files as Record<string, ExcalidrawFile>)
    : null
  const appState = obj.appState && typeof obj.appState === 'object' && !Array.isArray(obj.appState)
    ? (obj.appState as ExcalidrawScene['appState'])
    : undefined
  return {
    type: typeof obj.type === 'string' ? obj.type : undefined,
    version: typeof obj.version === 'number' ? obj.version : undefined,
    elements: (obj.elements as ExcalidrawElement[]).filter(
      (el): el is ExcalidrawElement => !!el && typeof el === 'object' && !Array.isArray(el),
    ),
    appState,
    files,
  }
}

/** Elements that should actually be painted. */
function visibleElements(scene: ExcalidrawScene): ExcalidrawElement[] {
  return scene.elements.filter(el => !el.isDeleted && DRAWABLE.has(String(el.type)))
}

/* ── geometry ─────────────────────────────────────────────────────────── */

export interface Bounds { minX: number; minY: number; maxX: number; maxY: number }

const num = (v: unknown, fallback = 0): number =>
  typeof v === 'number' && Number.isFinite(v) ? v : fallback

/**
 * Absolute, unrotated points that bound an element: its box corners plus any
 * relative `points`.
 *
 * Linear elements (line/arrow/freedraw) carry `points` relative to x/y and those
 * routinely go negative, so the extent has to come from the points rather than
 * from width/height alone.
 */
function elementExtentPoints(el: ExcalidrawElement): [number, number][] {
  const x = num(el.x), y = num(el.y)
  const w = num(el.width), h = num(el.height)
  const pts: [number, number][] = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
  if (Array.isArray(el.points)) {
    for (const p of el.points) {
      if (!Array.isArray(p)) continue
      pts.push([x + num(p[0]), y + num(p[1])])
    }
  }
  return pts
}

/**
 * Axis-aligned bounds over all visible elements, used for the viewBox.
 *
 * Rotation is applied about each element's own centre at render time, so the
 * bounds must cover the ROTATED extent. Measuring the unrotated box instead
 * clips the diagram: a 100x50 rectangle turned 90 degrees occupies 50x100, and
 * the corners that sweep outside the original box fall off the viewBox.
 */
export function sceneBounds(scene: ExcalidrawScene): Bounds | null {
  const els = visibleElements(scene)
  if (els.length === 0) return null
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const el of els) {
    const angle = num(el.angle)
    const cx = num(el.x) + num(el.width) / 2
    const cy = num(el.y) + num(el.height) / 2
    const cos = Math.cos(angle), sin = Math.sin(angle)
    for (const [px, py] of elementExtentPoints(el)) {
      let ax = px, ay = py
      if (angle) {
        const dx = px - cx, dy = py - cy
        ax = cx + dx * cos - dy * sin
        ay = cy + dx * sin + dy * cos
      }
      if (ax < minX) minX = ax
      if (ay < minY) minY = ay
      if (ax > maxX) maxX = ax
      if (ay > maxY) maxY = ay
    }
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null
  return { minX, minY, maxX, maxY }
}

/* ── palette ───────────────────────────────────────────────────────────────
 * Colours here are relative to the diagram's own painted canvas, NOT to the
 * dashboard theme. A scene is rendered as a self-contained picture — the way an
 * exported PNG would be — so the host theme must not restyle it. That is what
 * keeps an author's explicit colours faithful, and it is why there is no
 * `dark` option: there is nothing for a theme switch to invalidate. */

/** Canvas painted when a scene carries no `viewBackgroundColor`.
 *
 *  Excalidraw's own default canvas is white, and scenes authored there carry the
 *  editor's default near-black `strokeColor: "#1e1e1e"`. A minimal
 *  model-generated fence (`{"elements":[…]}`) has no background at all, so
 *  compositing straight onto the chat surface put near-black strokes on the dark
 *  dashboard and the diagram vanished. Painting the canvas the author actually
 *  saw keeps the scene legible under either theme and preserves their colours
 *  exactly — unlike inverting, which would also invert embedded images. */
const DEFAULT_CANVAS = '#ffffff'

/** Stroke/text fallback for an element with no usable colour of its own. */
const INK = '#1e1e1e'

/** Frame outline and label. */
const FRAME_CHROME = '#bbbbbb'

/* ── Excalidraw → rough.js option mapping ─────────────────────────────────
 * Mirrors Excalidraw's own `generateRoughOptions`, including the quirks: a
 * non-solid stroke gets +0.5 width and multi-stroke disabled (otherwise the
 * dashes double up and read as solid), and fill weight / hachure gap are
 * derived from stroke width. */

const dashed = (w: number) => [8, 8 + w]
const dotted = (w: number) => [1.5, 6 + w]

function roughOptionsFor(el: ExcalidrawElement): RoughOptions {
  const strokeWidth = num(el.strokeWidth, 1)
  const strokeStyle = typeof el.strokeStyle === 'string' ? el.strokeStyle : 'solid'
  const solid = strokeStyle === 'solid'
  const bg = safeColor(el.backgroundColor, 'transparent')
  const opts: RoughOptions = {
    seed: num(el.seed, 1) || 1,
    stroke: safeColor(el.strokeColor, INK),
    strokeWidth: solid ? strokeWidth : strokeWidth + 0.5,
    roughness: num(el.roughness, 1),
    fillWeight: strokeWidth / 2,
    hachureGap: strokeWidth * 4,
    disableMultiStroke: !solid,
  }
  if (bg !== 'transparent') {
    opts.fill = bg
    opts.fillStyle = typeof el.fillStyle === 'string' ? el.fillStyle : 'hachure'
  }
  if (strokeStyle === 'dashed') opts.strokeLineDash = dashed(strokeWidth)
  else if (strokeStyle === 'dotted') opts.strokeLineDash = dotted(strokeWidth)
  return opts
}

/**
 * Corner radius, matching Excalidraw's `getCornerRadius`.
 *
 * Two modes: proportional (a flat quarter of the shorter side) and adaptive
 * (proportional until the shape is big enough, then capped at a fixed 32) —
 * the cap is what stops a large rectangle looking like a stadium.
 */
const PROPORTIONAL_RADIUS = 0.25
const ADAPTIVE_RADIUS = 32

export function cornerRadius(shorterSide: number, el: ExcalidrawElement): number {
  const type = el.roundness?.type
  if (type === 2) return shorterSide * PROPORTIONAL_RADIUS
  if (type === 3) {
    const fixed = num(el.roundness?.value, ADAPTIVE_RADIUS) || ADAPTIVE_RADIUS
    const cutoff = fixed / PROPORTIONAL_RADIUS
    return shorterSide <= cutoff ? shorterSide * PROPORTIONAL_RADIUS : fixed
  }
  return 0
}

/** Rounded-rectangle path, in element-local coordinates.
 *
 * Built as a list of `[command, ...coords]` segments rather than interpolated
 * template strings. Slightly more verbose, but the geometry reads as data and
 * each command letter stays a discrete token instead of being buried in prose-
 * shaped text. */
function roundedRectPath(w: number, h: number, r: number): string {
  const rad = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2)
  const segments: (string | number)[][] = [
    ['M', rad, 0],
    ['L', w - rad, 0],
    ['Q', w, 0, w, rad],
    ['L', w, h - rad],
    ['Q', w, h, w - rad, h],
    ['L', rad, h],
    ['Q', 0, h, 0, h - rad],
    ['L', 0, rad],
    ['Q', 0, 0, rad, 0],
    ['Z'],
  ]
  return segments.map(seg => seg.join(' ')).join(' ')
}

/** Smooth an open polyline through its midpoints. Used for freedraw, which
 *  Excalidraw renders with perfect-freehand; a quadratic midpoint smooth gets
 *  the same soft feel without adding that dependency. */
function smoothPath(points: number[][]): string {
  const pts = points.filter(p => Array.isArray(p) && p.length >= 2)
  if (pts.length === 0) return ''
  if (pts.length === 1) return `M ${num(pts[0][0])} ${num(pts[0][1])}`
  if (pts.length === 2) {
    return `M ${num(pts[0][0])} ${num(pts[0][1])} L ${num(pts[1][0])} ${num(pts[1][1])}`
  }
  let d = `M ${num(pts[0][0])} ${num(pts[0][1])}`
  for (let i = 1; i < pts.length - 1; i++) {
    const cx = num(pts[i][0]), cy = num(pts[i][1])
    const mx = (cx + num(pts[i + 1][0])) / 2
    const my = (cy + num(pts[i + 1][1])) / 2
    d += ` Q ${cx} ${cy} ${mx} ${my}`
  }
  const last = pts[pts.length - 1]
  d += ` L ${num(last[0])} ${num(last[1])}`
  return d
}

/* ── text ─────────────────────────────────────────────────────────────── */

/** Excalidraw font family ids → CSS stacks. We ship none of Excalidraw's fonts,
 *  so each maps to the nearest system stack; the hand-drawn ids fall back to a
 *  casual face rather than defaulting to a neutral sans. */
const FONT_STACKS: Record<number, string> = {
  1: '"Excalifont", "Virgil", "Segoe Print", "Bradley Hand", "Comic Sans MS", cursive',
  2: '"Nunito", Helvetica, Arial, sans-serif',
  3: '"Cascadia Code", "Cascadia Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  5: '"Excalifont", "Virgil", "Segoe Print", "Comic Sans MS", cursive',
  6: '"Nunito", Helvetica, Arial, sans-serif',
  7: '"Lilita One", Impact, sans-serif',
  8: '"Comic Shanns", "Comic Sans MS", cursive',
}

function fontStack(id: unknown): string {
  return FONT_STACKS[num(id, 1)] || FONT_STACKS[1]
}

const TEXT_ANCHOR: Record<string, string> = { left: 'start', center: 'middle', right: 'end' }

/* ── arrowheads ───────────────────────────────────────────────────────── */

/** Draw an arrowhead at `(x, y)` pointing along `angle`. Returns nodes to append.
 *  Excalidraw supports several shapes; the common ones are covered and anything
 *  unrecognized simply draws nothing rather than guessing. */
function arrowheadNodes(
  rc: RoughSVG,
  kind: string,
  x: number,
  y: number,
  angle: number,
  opts: RoughOptions,
): SVGGElement[] {
  const size = Math.max(15, num(opts.strokeWidth, 1) * 8)
  const spread = Math.PI / 9 // 20°
  const solidOpts: RoughOptions = { ...opts, strokeLineDash: undefined, roughness: 0 }
  switch (kind) {
    case 'arrow': {
      const a = [x - size * Math.cos(angle - spread), y - size * Math.sin(angle - spread)]
      const b = [x - size * Math.cos(angle + spread), y - size * Math.sin(angle + spread)]
      return [
        rc.line(a[0], a[1], x, y, solidOpts),
        rc.line(b[0], b[1], x, y, solidOpts),
      ]
    }
    case 'triangle': {
      const a = [x - size * Math.cos(angle - spread), y - size * Math.sin(angle - spread)]
      const b = [x - size * Math.cos(angle + spread), y - size * Math.sin(angle + spread)]
      return [rc.polygon([[x, y], [a[0], a[1]], [b[0], b[1]]], {
        ...solidOpts, fill: solidOpts.stroke, fillStyle: 'solid',
      })]
    }
    case 'dot':
    case 'circle':
      return [rc.circle(x, y, size / 2, { ...solidOpts, fill: solidOpts.stroke, fillStyle: 'solid' })]
    case 'bar': {
      const half = size / 2
      const perp = angle + Math.PI / 2
      return [rc.line(
        x - half * Math.cos(perp), y - half * Math.sin(perp),
        x + half * Math.cos(perp), y + half * Math.sin(perp),
        solidOpts,
      )]
    }
    default:
      return []
  }
}

/* ── element drawing ──────────────────────────────────────────────────── */

function drawShape(rc: RoughSVG, el: ExcalidrawElement, opts: RoughOptions): SVGGElement | null {
  const w = num(el.width), h = num(el.height)
  switch (el.type) {
    case 'rectangle': {
      const r = cornerRadius(Math.min(Math.abs(w), Math.abs(h)), el)
      return r > 0
        ? rc.path(roundedRectPath(w, h, r), opts)
        : rc.rectangle(0, 0, w, h, opts)
    }
    case 'diamond':
      return rc.polygon([[w / 2, 0], [w, h / 2], [w / 2, h], [0, h / 2]], opts)
    case 'ellipse':
      return rc.ellipse(w / 2, h / 2, w, h, opts)
    default:
      return null
  }
}

function drawLinear(rc: RoughSVG, el: ExcalidrawElement, opts: RoughOptions): SVGGElement[] {
  const raw = Array.isArray(el.points) ? el.points : []
  const pts = raw
    .filter(p => Array.isArray(p) && p.length >= 2)
    .map(p => [num(p[0]), num(p[1])] as [number, number])
  if (pts.length < 2) return []

  const rounded = el.roundness?.type != null
  // preserveVertices keeps corners anchored so a multi-segment arrow still
  // meets its own joints after rough.js perturbs the path.
  const lineOpts: RoughOptions = { ...opts, preserveVertices: true }
  const nodes: SVGGElement[] = [
    rounded ? rc.curve(pts, lineOpts) : rc.linearPath(pts, lineOpts),
  ]

  if (el.type === 'arrow') {
    const last = pts[pts.length - 1]
    const prev = pts[pts.length - 2]
    const first = pts[0]
    const second = pts[1]
    // Default matches Excalidraw: an arrow has a head at the end unless the
    // scene says otherwise, and none at the start.
    const endKind = el.endArrowhead === undefined ? 'arrow' : el.endArrowhead
    if (endKind) {
      nodes.push(...arrowheadNodes(
        rc, String(endKind), last[0], last[1],
        Math.atan2(last[1] - prev[1], last[0] - prev[0]), opts,
      ))
    }
    if (el.startArrowhead) {
      nodes.push(...arrowheadNodes(
        rc, String(el.startArrowhead), first[0], first[1],
        Math.atan2(first[1] - second[1], first[0] - second[0]), opts,
      ))
    }
  }
  return nodes
}

function drawText(doc: Document, el: ExcalidrawElement): SVGGElement {
  const g = doc.createElementNS(SVG_NS, 'g') as SVGGElement
  const fontSize = num(el.fontSize, 20)
  const lineHeight = num(el.lineHeight, 1.25) || 1.25
  const lineHeightPx = fontSize * lineHeight
  const align = typeof el.textAlign === 'string' ? el.textAlign : 'left'
  const w = num(el.width)
  const xOffset = align === 'center' ? w / 2 : align === 'right' ? w : 0
  const lines = String(el.text ?? '').split('\n')

  for (let i = 0; i < lines.length; i++) {
    const t = doc.createElementNS(SVG_NS, 'text')
    t.setAttribute('x', String(xOffset))
    // 0.8em down from the line box top approximates the ascender, which is
    // where Excalidraw puts the baseline via canvas text metrics.
    t.setAttribute('y', String(i * lineHeightPx + fontSize * 0.8))
    t.setAttribute('font-family', fontStack(el.fontFamily))
    t.setAttribute('font-size', `${fontSize}px`)
    t.setAttribute('fill', safeColor(el.strokeColor, INK))
    t.setAttribute('text-anchor', TEXT_ANCHOR[align] || 'start')
    t.setAttribute('style', 'white-space:pre')
    // textContent, never innerHTML — text is untrusted scene content.
    t.textContent = lines[i]
    g.appendChild(t)
  }
  return g
}

function drawImage(
  doc: Document,
  el: ExcalidrawElement,
  scene: ExcalidrawScene,
): SVGGElement | null {
  const file = el.fileId ? scene.files?.[el.fileId] : undefined
  const href = safeImageDataUrl(file?.dataURL)
  if (!href) return null
  const g = doc.createElementNS(SVG_NS, 'g') as SVGGElement
  const img = doc.createElementNS(SVG_NS, 'image')
  img.setAttribute('x', '0')
  img.setAttribute('y', '0')
  img.setAttribute('width', String(num(el.width)))
  img.setAttribute('height', String(num(el.height)))
  img.setAttribute('preserveAspectRatio', 'none')
  img.setAttributeNS('http://www.w3.org/1999/xlink', 'href', href)
  img.setAttribute('href', href)
  g.appendChild(img)
  return g
}

function drawFrame(
  doc: Document,
  rc: RoughSVG,
  el: ExcalidrawElement,
): SVGGElement {
  const g = doc.createElementNS(SVG_NS, 'g') as SVGGElement
  const stroke = FRAME_CHROME
  g.appendChild(rc.rectangle(0, 0, num(el.width), num(el.height), {
    stroke, strokeWidth: 1, roughness: 0, seed: num(el.seed, 1) || 1,
  }))
  if (el.name) {
    const label = doc.createElementNS(SVG_NS, 'text')
    label.setAttribute('x', '0')
    label.setAttribute('y', '-8')
    label.setAttribute('font-size', '12px')
    label.setAttribute('font-family', FONT_STACKS[2])
    label.setAttribute('fill', stroke)
    label.textContent = String(el.name)
    g.appendChild(label)
  }
  return g
}

/* ── top-level render ─────────────────────────────────────────────────── */

export interface RenderOptions {
  /** Padding around the scene bounds, in scene units. */
  padding?: number
  /** Injectable for tests; defaults to the ambient document. */
  document?: Document
}

/** Empty-scene sentinel so callers can distinguish "nothing to draw" from
 *  "failed to parse" — the first is legitimate, the second is not.
 *
 *  Carries no message on purpose: it is caught and turned into the raw-source
 *  fallback, never displayed, so the class name is the whole signal. */
export class EmptySceneError extends Error {}

/**
 * Render a scene to a detached `<svg>` element.
 *
 * Throws `EmptySceneError` when there is nothing drawable. Callers are expected
 * to catch and fall back to the raw source.
 */
export function renderExcalidrawScene(
  scene: ExcalidrawScene,
  { padding = 16, document: doc = document }: RenderOptions = {},
): SVGSVGElement {
  const bounds = sceneBounds(scene)
  if (!bounds) throw new EmptySceneError()

  const width = Math.max(1, bounds.maxX - bounds.minX + padding * 2)
  const height = Math.max(1, bounds.maxY - bounds.minY + padding * 2)

  const svg = doc.createElementNS(SVG_NS, 'svg') as SVGSVGElement
  svg.setAttribute('xmlns', SVG_NS)
  svg.setAttribute(
    'viewBox',
    `${bounds.minX - padding} ${bounds.minY - padding} ${width} ${height}`,
  )
  // Width/height as attributes plus a max-width style: the diagram scales down
  // to the chat column but never blows past its natural size.
  svg.setAttribute('width', String(width))
  svg.setAttribute('height', String(height))
  svg.setAttribute('style', 'max-width:100%;height:auto')
  // Stable hook for callers and tests to identify a rendered diagram. `role` is
  // conditional (see below), so it cannot serve that purpose.
  svg.setAttribute('data-excalidraw-scene', '')

  // `role="img"` hides descendants from assistive tech, which would silently drop
  // the diagram's own text labels — the only part of a sketch that carries
  // readable meaning. Name the graphic with that text instead. With no text there
  // is nothing to name it with, so the role is omitted rather than shipping an
  // unnamed image that announces as "graphic" and nothing more.
  const spokenText = visibleElements(scene)
    .filter(el => el.type === 'text')
    .map(el => String(el.text ?? '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join('. ')
  if (spokenText) {
    svg.setAttribute('role', 'img')
    svg.setAttribute('aria-label', spokenText)
  }

  // Always painted, never composited onto the host surface. A transparent
  // canvas cannot guarantee the diagram is visible — that depends on whatever
  // is behind it — and guaranteeing visibility is the one job of a viewer. An
  // explicit `transparent` is therefore treated as "unspecified" too.
  const requested = safeColor(scene.appState?.viewBackgroundColor, DEFAULT_CANVAS)
  const bg = requested === 'transparent' ? DEFAULT_CANVAS : requested
  {
    const rect = doc.createElementNS(SVG_NS, 'rect')
    rect.setAttribute('x', String(bounds.minX - padding))
    rect.setAttribute('y', String(bounds.minY - padding))
    rect.setAttribute('width', String(width))
    rect.setAttribute('height', String(height))
    rect.setAttribute('fill', bg)
    svg.appendChild(rect)
  }

  const rc = rough.svg(svg)

  for (const el of visibleElements(scene)) {
    const opts = roughOptionsFor(el)
    let node: SVGGElement | null = null
    const children: SVGGElement[] = []

    switch (el.type) {
      case 'rectangle':
      case 'diamond':
      case 'ellipse':
        node = drawShape(rc, el, opts)
        break
      case 'line':
      case 'arrow':
        children.push(...drawLinear(rc, el, opts))
        break
      case 'freedraw': {
        const d = smoothPath(Array.isArray(el.points) ? el.points : [])
        if (d) {
          const p = doc.createElementNS(SVG_NS, 'path')
          p.setAttribute('d', d)
          p.setAttribute('fill', 'none')
          p.setAttribute('stroke', String(opts.stroke))
          p.setAttribute('stroke-width', String(num(opts.strokeWidth, 1) * 1.5))
          p.setAttribute('stroke-linecap', 'round')
          p.setAttribute('stroke-linejoin', 'round')
          const g = doc.createElementNS(SVG_NS, 'g') as SVGGElement
          g.appendChild(p)
          node = g
        }
        break
      }
      case 'text':
        node = drawText(doc, el)
        break
      case 'image':
        node = drawImage(doc, el, scene)
        break
      case 'frame':
      case 'magicframe':
        node = drawFrame(doc, rc, el)
        break
    }

    const nodes = node ? [node, ...children] : children
    if (nodes.length === 0) continue

    // One wrapper per element carries the element's placement, rotation and
    // opacity, so the shape helpers can all work in local coordinates.
    const wrapper = doc.createElementNS(SVG_NS, 'g') as SVGGElement
    const x = num(el.x), y = num(el.y)
    const angle = num(el.angle)
    let transform = `translate(${x} ${y})`
    if (angle) {
      const cx = num(el.width) / 2
      const cy = num(el.height) / 2
      transform += ` rotate(${(angle * 180) / Math.PI} ${cx} ${cy})`
    }
    wrapper.setAttribute('transform', transform)
    const opacity = num(el.opacity, 100)
    if (opacity < 100) wrapper.setAttribute('opacity', String(Math.max(0, opacity) / 100))
    for (const n of nodes) wrapper.appendChild(n)
    svg.appendChild(wrapper)
  }

  return svg
}

/**
 * Convenience entry point: raw JSON text → `<svg>`.
 *
 * Throws on unparseable input or an empty scene; callers fall back to the raw
 * source, matching how `MermaidBlock` handles a parse failure.
 */
export function renderExcalidrawSource(raw: string, opts: RenderOptions = {}): SVGSVGElement {
  const scene = parseExcalidrawScene(raw)
  if (!scene) throw new Error('invalid Excalidraw scene JSON')
  return renderExcalidrawScene(scene, opts)
}
