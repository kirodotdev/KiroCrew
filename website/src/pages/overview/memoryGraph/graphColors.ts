/**
 * Colours for the memory graph. Memories take their topic cluster's colour, as
 * data maps do (DataMapPlot, Embedding Atlas): a muted hue set by where the
 * cluster sits around the map's centre, so neighbouring topics get related
 * hues. Kind is a shape, not a colour. Theme colours are read from the
 * design-system CSS variables so the canvas follows light and dark mode.
 */
import type { GraphBbox, GraphCluster, GraphKind } from '../../../api/client/memory'

export type Rgb = [number, number, number]
export type GraphColors = { dark: boolean; unclustered: Rgb; cluster: Rgb; background: string; ring: string; label: string }
export type TopicColors = { dot: Map<number, Rgb>; text: Map<number, string> }

/** cosmos.gl `PointShape` values: fact ●, lesson ■, experience ▲. */
export const KIND_SHAPE: Record<GraphKind, number> = { fact: 0, lesson: 1, episode: 2 }

/**
 * Each kind's point size against a circle's, so every mark covers about the
 * same area and kind never reads as size. cosmos.gl draws a square 0.8 and a
 * triangle edge 0.9 of the point size, so an equal-area square is 0.886/0.8
 * of the circle's diameter and an equal-area triangle 1.5 of it; both are
 * nudged up a little for their rounded corners.
 */
export const KIND_SIZE: Record<GraphKind, number> = { fact: 1, lesson: 1.14, episode: 1.55 }

/** A CSS colour as 0-1 RGB, resolved by the browser itself (any syntax the theme uses). */
export function cssRgb(value: string, fallback: Rgb): Rgb {
  if (typeof document === 'undefined' || !value) return fallback
  // Painted onto one pixel and read back, so any colour syntax the theme uses
  // (oklch, color-mix, a nested var) comes out as sRGB.
  const ctx = document.createElement('canvas').getContext('2d', { willReadFrequently: true })
  if (!ctx) return fallback
  const probe = document.createElement('span')
  probe.style.color = value
  probe.style.display = 'none'
  document.body.appendChild(probe)
  ctx.fillStyle = getComputedStyle(probe).color
  probe.remove()
  ctx.fillRect(0, 0, 1, 1)
  // A context that cannot read pixels back (a stub) leaves the fallback.
  const px = ctx.getImageData?.(0, 0, 1, 1)?.data
  if (!px) return fallback
  const [r, g, b, a] = px
  if (a === 0) return fallback
  return [r / 255, g / 255, b / 255]
}

const luminance = ([r, g, b]: Rgb) => 0.2126 * r + 0.7152 * g + 0.0722 * b

export function readGraphColors(): GraphColors {
  const cs = getComputedStyle(document.documentElement)
  const v = (name: string) => cs.getPropertyValue(name).trim()
  const background = v('--bg-elevated') || '#1f2430'
  const muted = cssRgb(v('--muted'), [0.6, 0.62, 0.66])
  return {
    dark: luminance(cssRgb(background, [0.12, 0.14, 0.19])) < 0.5,
    unclustered: muted, cluster: muted, background, ring: v('--accent') || '#f97316', label: v('--text') || '#e5e7eb',
  }
}

/** OKLCH (L 0-1, C, hue in degrees) to sRGB 0-1, clipped into gamut. */
export function oklchToRgb(l: number, c: number, hDeg: number): Rgb {
  const h = (hDeg * Math.PI) / 180
  const a = c * Math.cos(h), b = c * Math.sin(h)
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3
  const lin = [
    4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
    -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
    -0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_,
  ]
  const gamma = (x: number) => (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055)
  return lin.map(x => Math.min(1, Math.max(0, gamma(x)))) as Rgb
}

const hex = (rgb: Rgb) => `#${rgb.map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('')}`

/** Muted on both themes: low chroma, lighter dots on dark. Label text is a
 *  deeper (light theme) or paler (dark theme) shade of its topic's dot. */
const DOT = { light: { l: 0.66, c: 0.11 }, dark: { l: 0.72, c: 0.1 } }
const TEXT = { light: { l: 0.42, c: 0.12 }, dark: { l: 0.86, c: 0.08 } }

/**
 * One colour per topic cluster: its hue is the cluster centre's angle around
 * the middle of the layout, so clusters that sit near each other (and so hold
 * similar memories) get similar hues; lightness steps a little with distance
 * from the middle so neighbours at the same angle still differ.
 */
export function topicColors(clusters: GraphCluster[], bounds: GraphBbox | undefined, dark: boolean): TopicColors {
  const dot = new Map<number, Rgb>(), text = new Map<number, string>()
  const cx = bounds ? (bounds[0] + bounds[2]) / 2 : 0, cy = bounds ? (bounds[1] + bounds[3]) / 2 : 0
  const half = bounds ? Math.max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / 2 || 1 : 1
  const d = dark ? DOT.dark : DOT.light, t = dark ? TEXT.dark : TEXT.light
  for (const c of clusters) {
    if (c.id < 0) continue
    const hue = ((Math.atan2(c.y - cy, c.x - cx) * 180) / Math.PI + 360) % 360
    const step = (Math.min(1, Math.hypot(c.x - cx, c.y - cy) / half) - 0.5) * 0.08
    dot.set(c.id, oklchToRgb(d.l + step, d.c, hue))
    text.set(c.id, hex(oklchToRgb(t.l, t.c, hue)))
  }
  return { dot, text }
}
