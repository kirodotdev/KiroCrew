/**
 * Dash fitting for the canvas's dashed outlines. FigJam's dash is long
 * with a shorter gap; here each straight side gets a whole number of dashes that
 * start and end flush with its ends, so the corners between sides are always
 * drawn. Pure.
 */

/** Dash and gap lengths in line widths, as measured on FigJam's outline. */
const DASH = 5.5
const GAP = 3

/**
 * A dash array for a straight run of *length*, whose first dash starts at its
 * start and whose last ends at its end. The gap stays as designed and the dash
 * stretches a little to make it fit; a run too short for two dashes is solid.
 */
export function fitDash(length: number, width: number): string | undefined {
  const dash = DASH * width
  const gap = GAP * width
  const n = Math.round((length + gap) / (dash + gap))
  if (n < 2) return undefined
  const fitted = (length - (n - 1) * gap) / n
  return `${fitted} ${gap}`
}

/** A dash array for a closed ellipse with radii *rx* and *ry*: n dashes and n gaps round the whole way. */
export function ovalDash(rx: number, ry: number, width: number): string | undefined {
  // Ramanujan's approximation of the perimeter.
  const perimeter = Math.PI * (3 * (rx + ry) - Math.sqrt((3 * rx + ry) * (rx + 3 * ry)))
  const dash = DASH * width
  const gap = GAP * width
  const n = Math.round(perimeter / (dash + gap))
  if (n < 3) return undefined
  const period = perimeter / n
  return `${period - gap} ${gap}`
}
