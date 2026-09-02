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

/**
 * Enforce the emitFile-over-publicDir precedence the overlay relies on.
 *
 * The edition overlay (editionExtensionPlugin.generateBundle) emits each
 * allowlisted `public/` asset with a pinned `fileName` on the assumption that an
 * emitted asset WINS over the same-named `publicDir` copy. That precedence is
 * empirical (verified on Vite 8 / Rolldown), not a documented contract: if a
 * future bundler upgrade flips it, an edition build would silently ship the
 * stock icons/manifest on a green build — and because edition builds run
 * downstream, outside this repo's CI, nothing automated would observe it. That
 * is exactly the silent-degrade class the seam's fail-loud contract bans.
 *
 * This turns the assumption into an enforced build-time invariant: after the
 * bundle is written, byte-compare each overlaid dist file against its edition
 * `public/` source and throw on any mismatch (or a missing dist file). Pure and
 * build-free: the caller injects a `readFile` (Buffer-returning, e.g.
 * `fs.readFileSync`) and a `join` (e.g. `path.join`), so it is unit-testable
 * without running a real build — the same layout as the rest of this module.
 *
 * @param {object} args
 * @param {string} args.distDir - the written bundle output dir (build.outDir).
 * @param {string} args.editionPublicDir - the edition's `public/` source dir.
 * @param {string[]} args.overlayFiles - the overlaid file names (allowlisted).
 * @param {(p: string) => Buffer} args.readFile - reads a file to a Buffer; may throw ENOENT.
 * @param {(...parts: string[]) => string} args.join - path join.
 * @throws {Error} fail-loud on a missing dist file or a byte mismatch.
 */
export function verifyOverlayBytes({ distDir, editionPublicDir, overlayFiles, readFile, join }) {
  for (const file of overlayFiles) {
    const src = readFile(join(editionPublicDir, file))
    let dist
    try {
      dist = readFile(join(distDir, file))
    } catch (e) {
      throw new Error(
        `[kirocrew-edition] overlaid asset '${file}' is missing from the build output ` +
          `(${join(distDir, file)}): ${e instanceof Error ? e.message : e}. ` +
          'The emitFile overlay should have written it — the emitFile-over-publicDir ' +
          'precedence this seam relies on may have changed.'
      )
    }
    if (!src.equals(dist)) {
      throw new Error(
        `[kirocrew-edition] overlaid asset '${file}' in the build output does not match the ` +
          `edition source (${join(editionPublicDir, file)}): the stock publicDir copy appears to ` +
          'have won over the emitted edition asset. The emitFile-over-publicDir precedence this ' +
          'seam relies on is not a documented bundler contract; a Vite/Rolldown upgrade may have ' +
          'flipped it. Edition builds run downstream, so this check is the only thing that catches it.'
      )
    }
  }
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
