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

/** A section label grows from its bottom-left corner as the canvas zooms out. */
export const LABEL_ORIGIN = 'left bottom'

/** The hairline around one colour dot in the swatch panel, so white and pale swatches still show. */
export const SWATCH_DOT_BORDER = '1px solid color-mix(in srgb, var(--text) 18%, transparent)'

/** A solid CSS border of *width* px in *color*. */
export const solidBorder = (width: number, color: string) => `${width}px solid ${color}`

/**
 * The sticker look: a shape is die-cut, with a white edge and a short drop
 * shadow under it, as if peeled and stuck onto the board. Drawn as box-shadow
 * so the edge sits outside the shape without moving its layout. The edge is
 * paper white in every theme, like the swatches it frames.
 */
export const STICKER_EDGE = '#FFFFFF'
export const STICKER_EDGE_W = 5
/** A sticker's edge of *width* px in *color* (white unless the person picked one), and its lift. */
export const stickerShadow = (color: string, width: number) =>
  `0 0 0 ${width}px ${color}, 0 1px 0 ${width}px rgba(31,35,40,.08), 0 7px 16px -2px rgba(31,35,40,.32)`
export const STICKER_SHADOW = stickerShadow(STICKER_EDGE, STICKER_EDGE_W)
/** The same edge and lift for an SVG shape (the diamond), which box-shadow cannot follow. */
export const STICKER_DROP = 'drop-shadow(0 1px 0 rgba(31,35,40,.08)) drop-shadow(0 6px 8px rgba(31,35,40,.28))'
/** A section's rim: thinner than a sticker's edge, with a hairline so it reads on a light ground. */
export const SECTION_RIM = `0 0 0 2px ${STICKER_EDGE}, 0 0 0 3px rgba(31,35,40,.08)`
/** A section title's white tag. */
export const TAG_SHADOW = '0 1px 0 rgba(31,35,40,.06), 0 3px 8px -3px rgba(31,35,40,.35)'
/** Chrome that floats over the board: the dock, the toolbars, the menus. */
export const FLOAT_SHADOW = '0 1px 0 rgba(31,35,40,.06), 0 10px 26px -10px rgba(31,35,40,.38)'
/** An active control as a sticker: the accent with an edge of the panel it sits on. */
export const ACTIVE_STICKER = '0 0 0 3px var(--bg-elevated), 0 0 0 4px var(--accent), 0 4px 10px -4px color-mix(in srgb, var(--accent) 70%, transparent)'

/**
 * A sticky note is paper, not a sticker: square corners, the top-right corner
 * folded over, a strip of tape across the top and a soft lift. The shadow is a
 * filter so it follows the folded outline, which box-shadow cannot.
 */
export const NOTE_DROP = 'drop-shadow(0 1px 1px rgba(31,35,40,.10)) drop-shadow(0 7px 8px rgba(31,35,40,.28))'
/** The paper with its top-right corner cut away, where the fold sits. */
export const noteClip = (fold: number) => `polygon(0 0, calc(100% - ${fold}px) 0, 100% ${fold}px, 100% 100%, 0 100%)`
/** The folded-over corner: a triangle in the note's darker shade. */
export const noteFold = (shade: string) => `linear-gradient(225deg, transparent 50%, ${shade} 50%)`
/** Translucent tape, the same in every theme because it sits on the paper. */
export const TAPE = 'rgba(255,255,255,.72)'
export const TAPE_SHADOW = '0 1px 2px rgba(0,0,0,.14)'

/** *color* at 40% over transparent: a shape's see-through fill. */
export const seeThrough = (color: string) => `color-mix(in srgb, ${color} 40%, transparent)`
