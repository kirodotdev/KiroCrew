/**
 * A canvas's default styles: the look a new object starts with, one per group
 * of kinds. Outline shapes (rectangle, ellipse, diamond) share one, stickies
 * have one and text boxes have one; a section has none. A group with no default
 * starts in the built-in look. Setting a default never restyles what is
 * already drawn.
 *
 * A default holds only style fields that differ from the built-in look, so
 * setting one from an object in the built-in look is the same as resetting it.
 */

import { toHex } from './colorMath'
import { alignOf, fontSizeOf, lookOf, textStyleOf, toSwatch, type Shape, type ShapeKind, type Swatch } from './canvasModel'

export type StyleGroup = 'shape' | 'sticky' | 'text'
export const STYLE_GROUPS: readonly StyleGroup[] = ['shape', 'sticky', 'text']

export type ShapeStyle = Partial<Pick<Shape,
  'color' | 'hex' | 'fill' | 'weight' | 'border' | 'borderColor' | 'borderHex' | 'ink' | 'inkHex' | 'fontSize' | 'bold' | 'strike' | 'align'>>
export type CanvasDefaults = Partial<Record<StyleGroup, ShapeStyle>>

/** The group a kind's new objects take their default from; null for a section. */
export function groupOf(kind: ShapeKind): StyleGroup | null {
  if (kind === 'sticky' || kind === 'text') return kind
  return kind === 'section' ? null : 'shape'
}

/** The built-in colour a new object of *kind* starts with. */
export const builtinColor = (kind: ShapeKind): Swatch => (kind === 'sticky' ? 'yellow' : kind === 'text' || kind === 'section' ? 'smoke' : 'sky')

/** One kind standing for its group: the three outline shapes share every style default. */
const GROUP_KIND: Record<StyleGroup, ShapeKind> = { shape: 'rect', sticky: 'sticky', text: 'text' }

type Obj = Readonly<Record<string, unknown>>

/**
 * The style fields of *o* that its group draws, valid and not built in: a text
 * box has no fill or outline, a sticky no outline, and only a text box has
 * bold, strikethrough and its own ink.
 */
export function readStyle(group: StyleGroup, o: Obj): ShapeStyle {
  const kind = GROUP_KIND[group]
  const color = toSwatch(o.color)
  const out: ShapeStyle = { ...alignOf(kind, o.align), ...fontSizeOf(kind, o.fontSize) }
  if (group === 'text') return { ...textStyleOf(o), ...out }
  const look = lookOf(o)
  const hex = toHex(o.hex)
  return {
    ...(color && color !== builtinColor(kind) ? { color } : {}),
    ...(hex ? { hex } : {}),
    ...(look.fill ? { fill: look.fill } : {}),
    ...(group === 'shape' ? {
      ...(look.weight ? { weight: look.weight } : {}),
      ...(look.border ? { border: look.border } : {}),
      ...(look.borderColor ? { borderColor: look.borderColor } : {}),
      ...(look.borderHex ? { borderHex: look.borderHex } : {}),
    } : {}),
    ...out,
  }
}

/** The one group every shape in *shapes* belongs to, or null when they mix groups or have none. */
export function sharedGroup(shapes: readonly Shape[]): StyleGroup | null {
  const group = shapes.length ? groupOf(shapes[0].kind) : null
  return group && shapes.every(s => groupOf(s.kind) === group) ? group : null
}

/** The default *shapes* can set together: one group and one look, else null. */
export function sharedStyle(shapes: readonly Shape[]): { group: StyleGroup; style: ShapeStyle } | null {
  const group = sharedGroup(shapes)
  if (!group) return null
  const style = readStyle(group, shapes[0] as unknown as Obj)
  const key = JSON.stringify(style)
  return shapes.every(s => JSON.stringify(readStyle(group, s as unknown as Obj)) === key) ? { group, style } : null
}

/** The defaults stored in a file, or undefined when it holds none it can use. */
export function readDefaults(v: unknown): CanvasDefaults | undefined {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined
  const out: CanvasDefaults = {}
  for (const g of STYLE_GROUPS) {
    const raw = (v as Obj)[g]
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const style = readStyle(g, raw as Obj)
    if (Object.keys(style).length) out[g] = style
  }
  return Object.keys(out).length ? out : undefined
}

/** *defaults* with *group* set to *style*, or cleared when *style* is null or built in. */
export function withDefault(defaults: CanvasDefaults | undefined, group: StyleGroup, style: ShapeStyle | null): CanvasDefaults | undefined {
  const next: CanvasDefaults = {}
  // Groups stay in one order, so two equal sets of defaults serialize the same.
  for (const g of STYLE_GROUPS) {
    const value = g === group ? (style && Object.keys(style).length ? style : undefined) : defaults?.[g]
    if (value) next[g] = value
  }
  return Object.keys(next).length ? next : undefined
}

/**
 * The style fields a new *kind* object starts with: its group's default over the
 * built-in colour. A custom fill colour in the default overrides the swatch.
 */
export function newShapeStyle(defaults: CanvasDefaults | undefined, kind: ShapeKind): Pick<Shape, 'color'> & ShapeStyle {
  const group = groupOf(kind)
  return { color: builtinColor(kind), ...(group ? defaults?.[group] : undefined) }
}
