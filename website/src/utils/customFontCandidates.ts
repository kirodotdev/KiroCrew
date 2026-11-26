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
]

/**
 * Regional CJK reading faces, one list per script so the picker can lead with
 * the UI language's own faces. Each list runs macOS/iOS, then Windows, then the
 * cross-platform and Linux faces. Japanese lists Meiryo ahead of Yu Gothic:
 * Windows draws Yu Gothic Regular thin and faint at body sizes, which is what
 * Japanese readers report as hard to read.
 */
const JAPANESE_FONT_CANDIDATES = [
  'Hiragino Sans',
  'Hiragino Kaku Gothic ProN',
  'Meiryo',
  'Meiryo UI',
  'Yu Gothic UI',
  'Yu Gothic',
  'Yu Gothic Medium',
  'BIZ UDPGothic',
  'Noto Sans JP',
  'Noto Sans CJK JP',
  'Source Han Sans JP',
  'IPAexGothic',
]
const SIMPLIFIED_CHINESE_FONT_CANDIDATES = [
  'PingFang SC',
  'Hiragino Sans GB',
  'Microsoft YaHei',
  'Microsoft YaHei UI',
  'Noto Sans SC',
  'Noto Sans CJK SC',
  'Source Han Sans SC',
  'WenQuanYi Micro Hei',
]
const TRADITIONAL_CHINESE_FONT_CANDIDATES = [
  'PingFang TC',
  'PingFang HK',
  'Microsoft JhengHei',
  'Microsoft JhengHei UI',
  'Noto Sans TC',
  'Noto Sans CJK TC',
  'Source Han Sans TC',
]
const KOREAN_FONT_CANDIDATES = [
  'Apple SD Gothic Neo',
  'Malgun Gothic',
  'Noto Sans KR',
  'Noto Sans CJK KR',
  'Source Han Sans KR',
  'Nanum Gothic',
]

/**
 * Every family name worth probing for the custom-font picker, deduplicated and in
 * display order: proportional families first (the common case for a reading
 * surface), then the regional CJK faces, then the monospace candidates (Nerd Font
 * builds included) for a user who wants a coding or ligature font.
 */
export const CUSTOM_FONT_CANDIDATES: readonly string[] = Array.from(
  new Set([
    ...PROPORTIONAL_FONT_CANDIDATES,
    ...JAPANESE_FONT_CANDIDATES,
    ...SIMPLIFIED_CHINESE_FONT_CANDIDATES,
    ...TRADITIONAL_CHINESE_FONT_CANDIDATES,
    ...KOREAN_FONT_CANDIDATES,
    ...MONO_FONT_CANDIDATES,
  ]),
)

/** The regional list for a BCP-47 UI language, or null outside ja/zh/ko. */
function regionalCandidates(language: string): readonly string[] | null {
  const tag = language.toLowerCase()
  if (tag === 'ja' || tag.startsWith('ja-')) return JAPANESE_FONT_CANDIDATES
  if (tag === 'ko' || tag.startsWith('ko-')) return KOREAN_FONT_CANDIDATES
  if (/^zh-(tw|hk|mo|hant)\b/.test(tag)) return TRADITIONAL_CHINESE_FONT_CANDIDATES
  if (tag === 'zh' || tag.startsWith('zh-')) return SIMPLIFIED_CHINESE_FONT_CANDIDATES
  return null
}

/**
 * `CUSTOM_FONT_CANDIDATES` with the UI language's own CJK faces moved to the
 * top, so a Japanese reader opening the picker sees Meiryo and Hiragino first
 * instead of below forty Latin faces.
 */
export function customFontCandidatesFor(language: string): readonly string[] {
  const lead = regionalCandidates(language)
  return lead ? Array.from(new Set([...lead, ...CUSTOM_FONT_CANDIDATES])) : CUSTOM_FONT_CANDIDATES
}
