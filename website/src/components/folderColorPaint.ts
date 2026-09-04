/* Folder-glyph color paints (data-only module — no UI copy).
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
