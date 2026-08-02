/* Folder-glyph BACK-panel paint plumbing (data-only module — no UI copy).
 *
 * The sidebar's FolderGlyph draws its back panel by CLIPPING plain spans with
 * CSS `mask-image` data-URIs — deliberately NOT an inline <svg> icon: the icon
 * system is lucide-only (AUTOSDE `use-lucide-icons`), and this is shape
 * plumbing for a styled container, the same primitive family as the front
 * panel's bordered rounded rect.
 *
 * This lives in its own module because every string in it is SVG markup /
 * path data, never user-visible copy — the module is excluded by name in
 * eslint.i18n.config.js (same named-boundary idiom as `*.prompt.ts`), which
 * keeps ChatSidebar.tsx itself fully covered by the i18n literal gate. */

/** Outline weight shared by every panel edge so the glyph reads as one
 *  drawing: the front uses a plain CSS border; the back paints its outline
 *  through a stroked mask generated at the exact pixel size (below), so its
 *  width is uniform along the curves and matches the front.
 *
 *  PROPORTIONAL, like a real lucide icon: lucide keeps a constant
 *  stroke-to-canvas ratio (2/24), so its icons read equally chunky at every
 *  the same var(--muted) family as the rail icons: the stroke paints via
 *  currentColor, so the glyph root's text color drives it — muted/70 at rest,
 *  full muted when the row is hovered (see FolderGlyph's default className).
 *  Matching color at mismatched weights still reads as different grays, so
 *  the ratio is the color match too. */
export const FOLDER_OUTLINE_RATIO = 2 / 24

/** Outline width in px for a glyph w px wide. */
export function folderOutlinePx(w: number): number {
  return Math.round(FOLDER_OUTLINE_RATIO * w * 100) / 100
}

/** Front-panel border width: the back's masked stroke reads a touch heavier
 *  than a plain CSS border at the same nominal width (mask-edge antialiasing
 *  on both sides of the ring vs a border's single crisp edge), so the front
 *  carries a small boost to match optically — and to fully cover the back's
 *  body-edge stroke, which shares the front's top line. */
export function folderFrontOutlinePx(w: number): number {
  return Math.round(FOLDER_OUTLINE_RATIO * w * 1.3 * 100) / 100
}

export const FOLDER_BACK_FILL = 'var(--bg-elevated)'

/* Colored-folder paints — a folder's identity mark is its palette color.
 * Stroke darkens the palette color toward
 * text-strong so linework keeps rail-icon contrast; the faces take a light
 * wash over the same surface gray (front slightly stronger, preserving the
 * two-panel depth). CSS value plumbing, never user-visible copy — this module
 * is name-excluded from the i18n literal gates. */
export function folderColorStroke(c: string): string {
  return `color-mix(in srgb, ${c} 75%, var(--text-strong))`
}
export function folderColorBackFill(c: string): string {
  return `color-mix(in srgb, ${c} 10%, var(--bg-elevated))`
}
export function folderColorFrontFill(c: string): string {
  return `color-mix(in srgb, ${c} 18%, var(--bg-elevated))`
}

/* BACK-panel silhouette — one continuous hand-authored path on a 56×42 grid
 * (insets match the front's 2% margins), "lucide-ized": corner radii widened
 * to lucide's r=2-at-24 proportion and the tab's notch re-sloped to the stock
 * Folder glyph's diagonal, so the closed folder reads as kin to the rest of
 * the icon set. The body's top edge sits at y=9.2 — the SAME line as the
 * front's top (22% of 42) — so its stroke hides exactly behind the front's
 * border and only the tab + notch (top y=4.3) show when closed: one line
 * weight, never a doubled edge. The body continues to the bottom, its lower
 * edges hidden behind the opaque front; the open tilt slides them out (the
 * folder mouth). */
const _FOLDER_BACK_PATH = 'M7 40L49 40C52.4 40 54 38.3 54 35L54 13.4C54 10.8 52.4 9.2 49.8 9.2L29.8 9.2C28.4 9.2 27.3 8.8 26.3 7.9L23.6 5.5C22.6 4.6 21.5 4.3 20.1 4.3L7 4.3C3.9 4.3 2 6.2 2 9.3L2 35C2 38.3 3.6 40 7 40Z'

/** Wrap a path in a fill/stroke mask pair. Coordinates are PIXELS and the
 *  image carries explicit width/height (no viewBox, nothing stretches), so
 *  the stroked outline keeps constant width along every bend. Callers nest
 *  the line layer inside the fill layer, clipping the stroke's outer half —
 *  the visible outline is inside-aligned at exactly folderOutlinePx(w). */
function _pathMasks(d: string, w: number, h: number): { fill: string; line: string } {
  const svg = (body: string) =>
    `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${h}'>${body}</svg>`)}")`
  return {
    fill: svg(`<path d='${d}' fill='white'/>`),
    // stroke straddles the path edge; the outer half is clipped by nesting.
    // Round caps/joins are half of lucide's character — keep them.
    line: svg(`<path d='${d}' fill='none' stroke='white' stroke-width='${2 * folderOutlinePx(w)}' stroke-linecap='round' stroke-linejoin='round'/>`),
  }
}

const _folderMaskCache = new Map<string, { fill: string; line: string }>()

/** Fill + stroke mask pair for the back-panel silhouette at w×h px. */
export function folderBackMasks(w: number, h: number): { fill: string; line: string } {
  const key = `back:${w}:${h}`
  const hit = _folderMaskCache.get(key)
  if (hit) return hit
  const sx = w / 56
  const sy = h / 42
  let i = 0
  const d = _FOLDER_BACK_PATH.replace(/-?\d+(?:\.\d+)?/g, m => {
    const n = parseFloat(m)
    return String(Math.round((i++ % 2 === 0 ? n * sx : n * sy) * 100) / 100)
  })
  const masks = _pathMasks(d, w, h)
  _folderMaskCache.set(key, masks)
  return masks
}
