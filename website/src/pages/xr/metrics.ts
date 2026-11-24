/**
 * How big things are, in degrees at the eye.
 *
 * Text on glasses is read by its angle, not by its pixels. Every surface has
 * a width in metres, a distance and a canvas width; from those this module
 * gives the canvas pixels a letter needs to reach an angle. Fonts are taken
 * from here, so a surface cannot be made smaller without its text failing
 * `metrics.test.ts`.
 */
export const DEG = Math.PI / 180

/** Smallest text allowed anywhere, body text, and the command of an approval. */
export const TEXT_FLOOR_DEG = 0.7
export const BODY_DEG = 0.75
export const COMMAND_DEG = 1.0

export interface SurfaceSpec {
  /** Width in metres. */
  width: number
  /** Distance from the eye in metres at which it is read. */
  distance: number
  /** Canvas width in pixels. */
  px: number
}

/** Canvas pixels per degree at the eye, at the centre of the surface. */
export function pxPerDeg(s: SurfaceSpec): number {
  return s.px / ((2 * Math.atan(s.width / 2 / s.distance)) / DEG)
}

/** The canvas font size that is `deg` tall on this surface. */
export const pxFor = (s: SurfaceSpec, deg: number): number => Math.ceil(deg * pxPerDeg(s))

/** The angle a canvas font size has on this surface. */
export const degOf = (s: SurfaceSpec, px: number): number => px / pxPerDeg(s)

/** A full panel: chat, dashboard, work, crew. Never scaled below 1. */
export const FULL: SurfaceSpec = { width: 0.46, distance: 0.9, px: 1024 }
/** A card next to a Bot: glance, came-over, draft, speech bubble. */
export const CARD: SurfaceSpec = { width: 0.32, distance: 0.8, px: 720 }
/**
 * The choice cards a Bot holds out: up to four in a row. It is sized so that one card of a full row is 10.5 by 6.5 cm
 * when it is 0.6 m away, which is 14 by 8.7 cm at this surface's own distance.
 */
export const CHOICE: SurfaceSpec = { width: 0.62, distance: 0.8, px: 1400 }
/** The hand strip, read at arm's length. */
export const STRIP: SurfaceSpec = { width: 0.3, distance: 0.5, px: 780 }

/** What a pair of glasses shows at once, in degrees across and down. */
export interface ViewSpec { h: number; v: number }

/** A view from the two numbers a vendor gives: the diagonal field of view and the pixels per eye. */
export function viewOf(diagonalDeg: number, pxW: number, pxH: number): ViewSpec {
  const perPx = Math.tan((diagonalDeg / 2) * DEG) / Math.hypot(pxW, pxH)
  return { h: (2 * Math.atan(perPx * pxW)) / DEG, v: (2 * Math.atan(perPx * pxH)) / DEG }
}

/**
 * XREAL Aura as the vendor documents it: 70 degrees diagonal, 1920 x 1200 per eye, so about 61 by 41 degrees.
 * shortcut: taken from the spec sheet, not measured; replace it with a measured view once a device is at hand.
 */
export const AURA_VIEW = viewOf(70, 1920, 1200)

export interface FontSet { small: number; body: number; title: number; big: number; command: number }

function fonts(s: SurfaceSpec): FontSet {
  return {
    small: pxFor(s, TEXT_FLOOR_DEG),
    body: pxFor(s, BODY_DEG),
    title: pxFor(s, 0.95),
    big: pxFor(s, 1.3),
    command: pxFor(s, COMMAND_DEG),
  }
}

export const FULL_FONT = fonts(FULL)
export const CARD_FONT = fonts(CARD)
