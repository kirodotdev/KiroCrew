/* Folder-glyph paints and paint geometry (data-only module — no UI copy).
 *
 * A folder's identity mark is its palette color; these helpers derive the
 * glyph's paints from it. Stroke: the color pulled toward text-strong so
 * linework keeps rail-icon contrast on every theme. Wash: a light tint of the
 * color over the theme's elevated surface, so the fill reads as "this theme's
 * surface, colored" rather than a flat paint chip.
 *
 * This lives in its own module because every string in it is a CSS value
 * template, never user-visible copy — the module is excluded by name in
 * eslint.i18n.config.js (same named-boundary idiom as `*.prompt.ts`), which
 * keeps FolderGlyph.tsx itself fully covered by the i18n literal gate. */

export function folderColorStroke(c: string): string {
  return `color-mix(in srgb, ${c} 75%, var(--text-strong))`
}

export function folderColorWash(c: string): string {
  return `color-mix(in srgb, ${c} 18%, var(--bg-elevated))`
}

/** Geometry of the disclosure chevron and the hole cut for it, in CSS px
 *  relative to the glyph box's top-left. Lucide's chevron path covers only the
 *  middle half of its viewBox, so its ink reaches about a quarter of `chevron`
 *  from the centre in either rotation. The centre sits 2.5px inside the box's
 *  bottom-right corner, so the ink stays within the box (it never crowds the
 *  folder name across the row's 4px gap) and the box width is unchanged. The
 *  hole clears the ink plus a 1px moat, small enough that the folder outline
 *  stays readable as a badged folder. */
export function folderDisclosureNotch(size: number): { chevron: number; chevronLeft: number; maskImage: string } {
  const chevron = Math.max(9, Math.round(size * 0.75))
  const centre = size - 2.5
  const radius = chevron * 0.25 + 1.5
  return {
    chevron,
    chevronLeft: centre - chevron / 2,
    maskImage: `radial-gradient(circle at ${centre}px ${centre}px, transparent ${radius}px, #000 ${radius + 0.5}px)`,
  }
}
