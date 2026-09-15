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

/** A font is fetched unless it is a system family the document only names.
 *  `fPath`/`fWeight`/`fStyle` with a non-local `origin` is a webfont request. */
function fontIsRemote(entry: unknown): boolean {
  if (!entry || typeof entry !== 'object') return false
  const f = entry as Record<string, unknown>
  if (typeof f.fPath === 'string' && f.fPath !== '') return true
  // `origin` 0 is "local / none"; 1-3 are Google, Adobe and a custom URL.
  return typeof f.origin === 'number' && f.origin !== 0
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
