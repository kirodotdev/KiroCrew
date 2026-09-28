/**
 * Does a Lottie document ask the player to FETCH something?
 *
 * A pack's `.json` is authored by whoever made the pack and imported from a
 * third-party gallery, and the importer only checks that the file is non-empty.
 * `lottie-web`'s SVG renderer resolves a document's `assets` and `fonts` by
 * REQUESTING them, so a clip carrying `{"u": "https://…", "p": "pixel.png"}`
 * makes the dashboard issue an attacker-chosen request from its own
 * authenticated origin the moment a crew wears that pack — which is the
 * feature's ordinary use, not an exotic combination.
 *
 * The light player only removes EXPRESSION evaluation. It does nothing about
 * asset URLs, and the inert-content policy the gateway puts on the per-slot
 * route does not cover the inlined body the Lottie tier renders. So the fetch
 * has to be refused here, before `loadAnimation`.
 *
 * A font entry is the same boundary for a second reason. For one of its remote
 * origins the player does not fetch a stylesheet, it BUILDS one: it writes an
 * `@font-face` rule out of the entry's own `fFamily` text and appends a
 * `<style>` to the SVG's `<defs>`. The renderer here is `renderer: 'svg'`, so
 * that SVG is inline in the dashboard document and the rule it carries is a
 * document stylesheet — a pack whose `fFamily` closes the rule and opens
 * another one restyles the whole page. Refusing the entry is what keeps that
 * text out of the DOM.
 *
 * REFUSE rather than strip. Stripping leaves a clip drawn with holes in it,
 * which reads as a corrupt pack while quietly keeping every other reference in
 * the document to audit; refusing hands the caller a load failure it already
 * knows how to answer — the seeded ghost.
 *
 * The predicate is deliberately CONSERVATIVE: a reference is allowed only when
 * it is provably inline. Anything this module cannot prove is inline counts as
 * remote, so a document shape nobody here anticipated is refused rather than
 * fetched. A pack that draws entirely with vector shapes — which every pack the
 * Companion's own editor produces does — carries no `assets` entries of this
 * kind at all and is unaffected.
 */

/** An embedded asset marks itself `e: 1` and carries its bytes in `p` as a data
 *  URI. Anything else — `e: 0`, a missing `e`, a `p` that is a path, a `u`
 *  directory prefix — is a request. */
function assetIsRemote(entry: unknown): boolean {
  if (!entry || typeof entry !== 'object') return false
  const a = entry as Record<string, unknown>
  // A precomp asset is a nested layer list, not a file: it has `layers` and no
  // `p`/`u`, so it fetches nothing.
  if (Array.isArray(a.layers) && a.p === undefined && a.u === undefined) return false
  // Nothing to fetch and nothing to prove.
  if (a.p === undefined && a.u === undefined) return false
  // A non-empty `u` is a directory prefix, which only exists to be joined onto a
  // path and requested.
  if (typeof a.u === 'string' && a.u !== '') return true
  if (a.e !== 1) return true
  return typeof a.p !== 'string' || !a.p.startsWith('data:')
}

/** The whole set of values the player itself reads as "this family is local":
 *  absent, `null`, the empty string, the `'n'` code, and the number `0`. Every
 *  other value — a known remote code, an unknown code, a value of a type the
 *  key is not supposed to hold — counts as remote, because a value this module
 *  cannot place is a value it cannot prove is inline. */
function fontOriginIsLocal(value: unknown): boolean {
  return value === undefined || value === null || value === '' || value === 'n' || value === 0
}

/** A font is fetched unless it is a system family the document only names.
 *
 *  The player's own local branch tests `fPath` for TRUTHINESS, not for being a
 *  string, so any truthy `fPath` — an object, an array, a number, `true` —
 *  reaches the branches that build a `<link>` or a `<style>` out of the entry.
 *  This predicate has to split the same way the player does, so it reads
 *  `fPath` as truthiness too, and reads both origin keys against the player's
 *  local set: `fOrigin` carries string codes (`'p'` Google, `'g'` a URL, `'t'`
 *  Typekit) and `origin` carries the numeric ones (1 Google, 2 Adobe, 3 a
 *  custom URL). */
function fontIsRemote(entry: unknown): boolean {
  if (!entry || typeof entry !== 'object') return false
  const f = entry as Record<string, unknown>
  if (f.fPath) return true
  if (!fontOriginIsLocal(f.fOrigin)) return true
  return !fontOriginIsLocal(f.origin)
}

/**
 * `true` when this parsed document would make the player request something.
 *
 * Total: handed junk, it answers `false` — a document that is not an object has
 * no assets to fetch, and `LottieRenderer` refuses it on its own for being
 * unparseable.
 */
export function referencesRemoteAsset(doc: unknown): boolean {
  if (!doc || typeof doc !== 'object') return false
  const d = doc as Record<string, unknown>
  if (Array.isArray(d.assets) && d.assets.some(assetIsRemote)) return true
  const fonts = d.fonts
  if (fonts && typeof fonts === 'object') {
    const list = (fonts as Record<string, unknown>).list
    if (Array.isArray(list) && list.some(fontIsRemote)) return true
  }
  return false
}
