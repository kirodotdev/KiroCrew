/**
 * How a shape is painted: its fill, outline colour, style and weight from
 * its swatch, fill mode, border and weight. One place, so every kind of shape agrees.
 */

import { SWATCH_STYLE, type Shape, type StrokeWeight, type Swatch } from './canvasModel'
import { STICKER_EDGE, seeThrough } from './canvasPaint'
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
  /**
   * Drawn as a sticker: a white die-cut edge and a drop shadow instead of a
   * coloured outline. Every solid shape is one until it is given an outline of
   * its own (a colour, a weight or dashes); a see-through or unfilled shape
   * keeps its outline, since an edge around nothing reads as a hole.
   */
  sticker: boolean
  /** A sticker's edge colour: white unless a border colour was picked. */
  edge: string
  /** A sticker's edge width, from the border weight; 0 when the border is set to none. */
  edgeWidth: number
}

/** Sticker edge widths per border weight, in canvas units. */
export const EDGE_WIDTH: Record<StrokeWeight, number> = { thin: 3, medium: 5, thick: 8 }

/** A swatch's paint: its fill, deeper border shade and ink; or a custom colour's, with the border made darker automatically. */
export function swatchStyle(color: Swatch, hex?: string): { paper: string; fill: string; stroke: string; ink: string } {
  return hex ? { paper: hex, fill: hex, stroke: darker(hex), ink: inkOn(hex) } : SWATCH_STYLE[color]
}

/** A picked outline colour as CSS. Black is the canvas's ink, so it reads on dark canvases too. */
const picked = (c: Swatch) => (c === 'black' ? 'var(--text)' : SWATCH_STYLE[c].paper)

/**
 * A text box's text colour as CSS. Black is the canvas's ink, so text set to black
 * still reads on a dark canvas; unset is the theme's text colour.
 */
export function textInk(s: Pick<Shape, 'ink' | 'inkHex'>): string {
  if (s.inkHex) return s.inkHex
  return s.ink ? picked(s.ink) : 'var(--text)'
}

export function shapeLook(s: Pick<Shape, 'color' | 'fill' | 'weight' | 'border' | 'borderColor' | 'hex' | 'borderHex'>): ShapeLook {
  const sw = swatchStyle(s.color, s.hex)
  const mode = s.fill ?? 'solid'
  // Unless the outline has a colour of its own, a filled shape outlines in a deeper
  // shade of its colour and an unfilled one in its colour itself.
  const own = s.borderHex ?? (s.borderColor ? picked(s.borderColor) : undefined)
  const border =
    s.border === 'none' ? 'transparent'
      : own ?? (mode === 'none' ? (s.hex ?? picked(s.color)) : sw.stroke)
  const base = { border, width: STROKE_WIDTH[s.weight ?? 'medium'], dashed: s.border === 'dashed', sticker: false, edge: STICKER_EDGE, edgeWidth: 0 }
  if (mode === 'none') return { ...base, fill: 'transparent', ink: 'var(--text)', solid: false }
  if (mode === 'transparent') return { ...base, fill: seeThrough(sw.fill), ink: 'var(--text)', solid: false }
  // A solid shape is a sticker: its border colour and weight paint the die-cut
  // edge (white by default). Only a dashed border turns it back into an outline.
  if (s.border !== 'dashed') {
    return {
      ...base, border: 'transparent', width: 0, sticker: true, fill: sw.fill, ink: sw.ink, solid: true,
      // The edge takes the picked colour as it is: black means black here, not the
      // theme's ink, which is light in dark mode.
      edge: s.borderHex ?? (s.borderColor ? SWATCH_STYLE[s.borderColor].paper : STICKER_EDGE), edgeWidth: s.border === 'none' ? 0 : EDGE_WIDTH[s.weight ?? 'medium'],
    }
  }
  return { ...base, fill: sw.fill, ink: sw.ink, solid: true }
}
