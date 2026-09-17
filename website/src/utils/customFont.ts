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

/**
 * Build a CSS `font-family` stack from the raw user input, or '' when empty (the
 * signal for useZoom to fall back to the default body stack rather than apply an
 * empty value).
 *
 * A multi-word family name MUST be quoted (`Comic Sans MS` is three unquoted
 * family tokens otherwise), and a generic `sans-serif` fallback is appended so a
 * typed-but-uninstalled family degrades to a readable proportional face rather
 * than the browser default serif.
 */
export function resolveCustomFontFamily(input: string): string {
  const raw = input.trim()
  if (!raw) return ''
  const tokens = raw.split(',').map(t => t.trim()).filter(Boolean)
  if (tokens.length === 0) return ''
  const quoted = tokens.map(t => {
    if (/^['"].*['"]$/.test(t)) return t
    return /\s/.test(t) ? `'${t}'` : t
  })
  const genericFallbacks = ['serif', 'sans-serif', 'monospace', 'system-ui', 'ui-monospace']
  const hasGeneric = tokens.some(t => genericFallbacks.includes(t.toLowerCase()))
  if (!hasGeneric) quoted.push('sans-serif')
  return quoted.join(', ')
}
