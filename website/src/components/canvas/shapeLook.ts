/**
 * How a shape is painted: its fill, outline colour, style and weight from
 * its swatch, fill mode, border and weight. One place, so every kind of shape agrees.
 */

import { SWATCH_STYLE, type Shape, type StrokeWeight, type Swatch } from './canvasModel'
import { seeThrough } from './canvasPaint'
import { darker, inkOn } from './colorMath'

/** Outline widths in canvas units, so they scale with zoom like FigJam's. */
export const STROKE_WIDTH: Record<StrokeWeight, number> = { thin: 2, medium: 3.5, thick: 6 }

export interface ShapeLook {
  /** CSS background of the shape's body. */
  fill: string
  /** CSS colour of its outline; transparent when it has none. */
  border: string
  /** Outline width in canvas units. */
  width: number
  /** Whether the outline is dashed. */
  dashed: boolean
  /** Text colour that reads on it. */
  ink: string
  /** Whether the body hides what is under it. */
  solid: boolean
}

/** A swatch's paint: its fill, deeper border shade and ink; or a custom colour's, with the border made darker automatically. */
export function swatchStyle(color: Swatch, hex?: string): { paper: string; fill: string; stroke: string; ink: string } {
  return hex ? { paper: hex, fill: hex, stroke: darker(hex), ink: inkOn(hex) } : SWATCH_STYLE[color]
}

/** A picked outline colour as CSS. Black is the canvas's ink, so it reads on dark canvases too. */
const picked = (c: Swatch) => (c === 'black' ? 'var(--text)' : SWATCH_STYLE[c].paper)

export function shapeLook(s: Pick<Shape, 'color' | 'fill' | 'weight' | 'border' | 'borderColor' | 'hex' | 'borderHex'>): ShapeLook {
  const sw = swatchStyle(s.color, s.hex)
  const mode = s.fill ?? 'solid'
  // Unless the outline has a colour of its own, a filled shape outlines in a deeper
  // shade of its colour and an unfilled one in its colour itself.
  const own = s.borderHex ?? (s.borderColor ? picked(s.borderColor) : undefined)
  const border =
    s.border === 'none' ? 'transparent'
      : own ?? (mode === 'none' ? (s.hex ?? picked(s.color)) : sw.stroke)
  const base = { border, width: STROKE_WIDTH[s.weight ?? 'medium'], dashed: s.border === 'dashed' }
  if (mode === 'none') return { ...base, fill: 'transparent', ink: 'var(--text)', solid: false }
  if (mode === 'transparent') return { ...base, fill: seeThrough(sw.fill), ink: 'var(--text)', solid: false }
  return { ...base, fill: sw.fill, ink: sw.ink, solid: true }
}
