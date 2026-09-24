/**
 * The "Custom" option of the app Font Family selector (useZoom).
 *
 * When the user picks Custom, they name ANY installed family and it becomes the
 * dashboard body font (--font-body) app-wide, exactly like the built-in Sans /
 * System choices but user-supplied — so chat, the composer, and sidebar session
 * and folder names all pick it up by inheritance, a Nerd Font renders its glyphs
 * everywhere, and index.css turns on ligatures while Custom is active. It is a
 * per-CLIENT choice (the font must be installed on the viewing machine), so
 * useZoom persists it in localStorage under this key rather than server config.
 */
export const CUSTOM_FONT_STORAGE_KEY = 'mc-custom-font'

/**
 * Whether the Custom font renders programming ligatures (calt/liga). On by
 * default — ligatures are the point of the coding fonts people pick here — with
 * a Settings toggle to turn them off. Only meaningful while Custom is active.
 */
export const CUSTOM_FONT_LIGATURES_STORAGE_KEY = 'mc-custom-font-ligatures'

import { cssFontFamilyToken } from './fontDetect'

/**
 * Build a CSS `font-family` stack from the raw user input, or '' when empty (the
 * signal for useZoom to fall back to the default body stack rather than apply an
 * empty value).
 *
 * Every non-generic family token is quoted with `cssFontFamilyToken` — not just
 * multi-word ones: a bare `Comic Sans MS` is three family tokens, and an
 * unquoted single token that is not a valid CSS identifier (`0xProto` and every
 * other digit-leading font-book name, a name colliding with a CSS keyword) is
 * dropped by the parser, making `--font-body` invalid at computed-value time and
 * resetting body size/line-height app-wide. Quoting always makes the token a
 * `<string>` family name, which has none of those constraints.
 *
 * `var(--script-fallbacks)` is inserted ahead of the generic tail so a
 * Latin-only custom family keeps the localized-glyph coverage every other
 * `--font-body` site declares (index.css `:root` and the `html:lang(...)` blocks
 * carry `KC Han/Japanese/Korean/Devanagari/Bengali Fallback` there); useZoom
 * writes this stack as an inline `--font-body` on <html>, which would otherwise
 * shadow those declarations and drop the aliases for a zh-CN/ja/ko/hi/bn user.
 *
 * A generic `sans-serif` tail is appended so a typed-but-uninstalled family
 * degrades to a readable proportional face rather than the browser default serif.
 */
export function resolveCustomFontFamily(input: string): string {
  const raw = input.trim()
  if (!raw) return ''
  const tokens = raw.split(',').map(t => t.trim()).filter(Boolean)
  if (tokens.length === 0) return ''
  const genericFallbacks = ['serif', 'sans-serif', 'monospace', 'system-ui', 'ui-monospace']
  const isGeneric = (t: string) => genericFallbacks.includes(t.toLowerCase())
  const quoted = tokens.map(t => {
    // A generic keyword must stay unquoted (quoting turns it into a family name).
    if (isGeneric(t)) return t.toLowerCase()
    // An already-quoted token is left as the author wrote it.
    if (/^'.*'$/.test(t) || /^".*"$/.test(t)) return t
    return cssFontFamilyToken(t)
  })
  // Localized-script coverage, matching every built-in --font-body stack
  // (FAMILY_MAP / index.css). It MUST sit ahead of any generic tail: a generic
  // (serif/sans-serif/…) matches every glyph, so a script-fallback token placed
  // after it would never be reached. Insert it before the first generic the user
  // supplied, else append it before the generic we add below. The var() has no
  // value at the canvas-measure sites, which harmlessly resolve to nothing there.
  const firstGenericIdx = quoted.findIndex(t => isGeneric(t))
  if (firstGenericIdx === -1) {
    quoted.push('var(--script-fallbacks)', 'sans-serif')
  } else {
    quoted.splice(firstGenericIdx, 0, 'var(--script-fallbacks)')
  }
  return quoted.join(', ')
}
