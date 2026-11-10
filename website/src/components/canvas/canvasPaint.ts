/**
 * The canvas's CSS paint: every value here is handed to the CSS parser as a
 * colour, shadow, border or transform origin, never shown as words. Kept apart
 * so the canvas's components stay fully gated for user-facing copy while these
 * values, which the string scanner reads as prose, live in one copy-free module.
 *
 * Data in, CSS string out: no JSX, no DOM, no translator import. Keep it that
 * way; anything a person reads belongs in the components, through the catalog.
 */

/** A connector's line colour until one is picked: the canvas's ink. */
export const CONN_INK = 'color-mix(in srgb, var(--text) 68%, var(--bg))'

/** The canvas's ground, FigJam's slightly grey paper, as the thumbnail paints it. */
export const THUMB_GROUND = 'color-mix(in srgb, var(--bg) 95%, var(--text))'

/** A plain sticky's lift off the canvas. */
export const STICKY_SHADOW = '0 1px 2px rgba(0,0,0,.12), 0 8px 20px -6px rgba(0,0,0,.22)'

/** A section label grows from its bottom-left corner as the canvas zooms out. */
export const LABEL_ORIGIN = 'left bottom'

/** The hairline around one colour dot in the dark swatch panel. */
export const SWATCH_DOT_BORDER = '1px solid rgba(255,255,255,.18)'

/** A solid CSS border of *width* px in *color*. */
export const solidBorder = (width: number, color: string) => `${width}px solid ${color}`

/** *color* at 40% over transparent: a shape's see-through fill. */
export const seeThrough = (color: string) => `color-mix(in srgb, ${color} 40%, transparent)`
