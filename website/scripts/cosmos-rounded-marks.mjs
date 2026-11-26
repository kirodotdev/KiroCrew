/**
 * Rounds the corners of cosmos.gl's square and triangle point marks, so the
 * memory graph's lesson ■ and experience ▲ match the rounded legend icons on
 * its kind pills. cosmos.gl draws each shape from a signed-distance function in
 * its point fragment shader and has no option for a corner radius, so this
 * build-time transform rewrites the two calls in that shader.
 *
 * A rounded shape is the same shape shrunk by `s` and grown back by a radius:
 * `s * sd(p / s) - r`. Shrinking by `s = 1 - k` pulls each edge in by `k` of
 * the shape's inradius, and `r = k * inradius` pushes it back out, so the mark
 * keeps its size and only its corners round. `k` is the radius as a share of
 * the inradius: small enough that a 3 px mark still reads as a square or a
 * triangle, large enough to show at 12 px.
 *
 * The triangle is also flipped: the point sprite's y runs down the screen, so
 * cosmos.gl's triangle drew pointing down, unlike the ▲ on the legend.
 *
 * shortcut: the dev server pre-bundles cosmos.gl without this transform, so
 * marks there keep sharp corners; only `vite build` (what ships) is rounded.
 * Upgrade if a dev-only difference ever matters.
 */

/** The shader text this patch expects; `@cosmos.gl/graph` is pinned in package.json. */
const ANCHOR = `    if (shape == SQUARE) return squareDistance(p);
    else if (shape == TRIANGLE) return triangleDistance(p);`

/** Corner radius as a share of the shape's inradius. */
export const CORNER_SHARE = 0.36

const K = CORNER_SHARE.toFixed(2)
const S = (1 - CORNER_SHARE).toFixed(2)
// The square's inradius is SQUARE_HALF_SIZE; an equilateral triangle's is its half edge / sqrt(3).
export const ROUNDED = `    if (shape == SQUARE) return ${S} * squareDistance(p / ${S}) - ${K} * SQUARE_HALF_SIZE;
    else if (shape == TRIANGLE) return ${S} * triangleDistance(vec2(p.x, -p.y) / ${S}) - ${K} * TRIANGLE_HALF_EDGE / sqrt(3.0);`

/** The patched module text, or an error when cosmos.gl's shader moved. */
export function roundMarks(code) {
  if (code.includes(ROUNDED)) return code
  if (!code.includes(ANCHOR)) {
    throw new Error('cosmos-rounded-marks: the point shader in @cosmos.gl/graph changed; update scripts/cosmos-rounded-marks.mjs for the new version.')
  }
  return code.replace(ANCHOR, ROUNDED)
}

/** Vite plugin: rewrite the shader in cosmos.gl's bundle at build time. */
export function cosmosRoundedMarksPlugin() {
  return {
    name: 'cosmos-rounded-marks',
    enforce: 'pre',
    transform(code, id) {
      if (!/[\\/]@cosmos\.gl[\\/]graph[\\/]dist[\\/]index\.js$/.test(id.split('?')[0])) return null
      return { code: roundMarks(code), map: null }
    },
  }
}
