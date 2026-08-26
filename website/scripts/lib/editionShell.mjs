// Pure helpers for the edition pre-boot shell seam: the <title> /
// <meta name="theme-color"> patch and the public-asset overlay allowlist.
//
// Split out from the Vite plugin (editionExtensionPlugin in vite.config.ts) so
// the parsing, validation, and HTML rewriting are testable without running a
// build — the same layout as bundleReport.mjs.
//
// Everything here fails LOUDLY on bad input. The edition seam's contract is
// fail-closed/fail-loud throughout: a typo in branding.json silently shipping a
// stock title would be exactly the class of silent edition-build degradation
// the seam exists to prevent.

/** The only branding.json keys an edition may set. Anything else throws. */
export const BRANDING_KEYS = ['title', 'themeColor']

/**
 * Files an edition's `public/` dir may overlay onto the built dist.
 *
 * Deliberately a fixed allowlist rather than "copy whatever is there": the
 * structural guarantee that an edition cannot overwrite index.html, sw.js, or
 * vendor/* lives HERE, not in reviewer vigilance. Widen it consciously.
 */
export const SHELL_OVERLAY_ALLOWLIST = ['manifest.json', 'icon-192.png', 'icon-512.png']

/**
 * Parse and validate an edition's branding.json text.
 *
 * Returns `{ title?, themeColor? }`. Throws with an actionable message on
 * malformed JSON, a non-object payload, an unknown key (typo guard — a typoed
 * key would otherwise silently no-op), or a non-string/empty value.
 */
export function parseBrandingConfig(text) {
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    throw new Error(`branding.json is not valid JSON: ${e instanceof Error ? e.message : e}`)
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('branding.json must be a JSON object, e.g. {"title": "Acme Crew"}')
  }
  for (const key of Object.keys(parsed)) {
    if (!BRANDING_KEYS.includes(key)) {
      throw new Error(
        `branding.json has an unknown key '${key}' (allowed: ${BRANDING_KEYS.join(', ')})`
      )
    }
    const value = parsed[key]
    if (typeof value !== 'string' || value.trim() === '') {
      throw new Error(`branding.json '${key}' must be a non-empty string`)
    }
  }
  return parsed
}

/** Minimal HTML escape for text/attribute interpolation into index.html. */
export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * Apply a parsed branding config to the index.html shell.
 *
 * Replaces the <title> text and the <meta name="theme-color"> content. Throws
 * when a tag the config wants to patch is missing — if upstream restructures
 * the shell, the edition build must fail rather than quietly ship a stock
 * title (the swVersionPlugin placeholder check follows the same rule).
 */
export function applyBrandingToHtml(html, branding) {
  let out = html
  if (branding.title) {
    const re = /<title([^>]*)>[\s\S]*?<\/title>/
    if (!re.test(out)) {
      throw new Error('branding.title is set but index.html has no <title> tag to patch')
    }
    // Replacement CALLBACK, not a replacement string: in a replacement string
    // `$1`/`$&` in the branding text would be expanded as capture references,
    // silently corrupting a title like "AI for $1". The callback inserts the
    // text literally; attrs carries any attributes the tag grows in the future.
    out = out.replace(re, (_m, attrs) => `<title${attrs}>${escapeHtml(branding.title)}</title>`)
  }
  if (branding.themeColor) {
    // Attribute-order tolerant: the hook may see the tag after other HTML
    // transforms have reprinted it (content= before name=, quote changes).
    const re = /(<meta\b(?=[^>]*name=["']theme-color["'])[^>]*\bcontent=["'])[^"']*(["'])/
    if (!re.test(out)) {
      throw new Error(
        'branding.themeColor is set but index.html has no <meta name="theme-color"> to patch'
      )
    }
    out = out.replace(re, (_m, pre, post) => `${pre}${escapeHtml(branding.themeColor)}${post}`)
  }
  return out
}
