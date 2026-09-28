/**
 * Candidate font families probed against the viewing machine's font book for the
 * "Custom" Font Family option's picker, and the sibling of `monoFontCandidates.ts`
 * for the terminal.
 *
 * Same mechanism, opposite scope. The browser cannot list the font book without
 * the permission-gated Local Font Access API (see `fontDetect.ts`), so the
 * no-permission path needs a list of names to ask about; a family absent from
 * this list is simply never offered, and the picker keeps free text so an
 * unlisted family is still reachable by typing it.
 *
 * Scope is deliberately NOT monospace-only: the custom app font is prose, so
 * proportional reading and UI faces are exactly what a user wants — the
 * terminal's fixed-grid reason to exclude them does not apply. The monospace
 * candidates are folded in too, both because a user is free to run the whole
 * dashboard in a coding font and because that list carries the Nerd Font builds
 * whose glyphs a custom app font can now render.
 *
 * FONT FAMILY NAMES ONLY. Every string here is matched by value against the
 * machine's font book — a translated name resolves to nothing and the surface
 * silently falls back — which is why this module is exempt from the untranslated
 * gate in `eslint.i18n.config.js`. Any interface copy for the picker belongs in
 * the catalog, not behind that exemption.
 */

import { MONO_FONT_CANDIDATES } from './monoFontCandidates'

/**
 * Common proportional families, ordered by how likely a reader is to have one so
 * the first screenful carries the common cases rather than an alphabetical
 * accident. Cross-OS: the shipped system UI faces first, then popular installed
 * reading/UI families, then serif faces, then CJK-capable proportional faces so
 * a CJK reader is not forced into fallback.
 */
const PROPORTIONAL_FONT_CANDIDATES = [
  // System UI / shipped-by-default faces.
  'system-ui',
  'Inter',
  'Segoe UI',
  'Segoe UI Variable',
  'SF Pro Text',
  'SF Pro',
  'Helvetica Neue',
  'Helvetica',
  'Arial',
  'Roboto',
  'Ubuntu',
  'Cantarell',
  'Noto Sans',
  'Open Sans',
  'Lato',
  'Source Sans 3',
  'Source Sans Pro',
  'Fira Sans',
  'IBM Plex Sans',
  'Work Sans',
  'Nunito',
  'Nunito Sans',
  'Public Sans',
  'Atkinson Hyperlegible',
  'Space Grotesk',
  'Verdana',
  'Tahoma',
  'Calibri',
  // Serif faces, for readers who prefer prose in a serif.
  'Georgia',
  'Times New Roman',
  'Charter',
  'Iowan Old Style',
  'Merriweather',
  'Source Serif 4',
  'Noto Serif',
  'PT Serif',
  // CJK-capable proportional faces.
  'Noto Sans CJK SC',
  'Source Han Sans',
  'PingFang SC',
  'Hiragino Kaku Gothic ProN',
  'Microsoft YaHei',
  'Yu Gothic',
  'Malgun Gothic',
]

/**
 * Every family name worth probing for the custom-font picker, deduplicated and in
 * display order: proportional families first (the common case for a reading
 * surface), then the monospace candidates (Nerd Font builds included) for a user
 * who wants a coding or ligature font.
 */
export const CUSTOM_FONT_CANDIDATES: readonly string[] = Array.from(
  new Set([...PROPORTIONAL_FONT_CANDIDATES, ...MONO_FONT_CANDIDATES]),
)
