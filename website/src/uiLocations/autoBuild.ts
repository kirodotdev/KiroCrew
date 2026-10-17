/**
 * The find_ui auto tier, as this bundle knows it.
 *
 * The auto tier is a build-time artifact (never committed), so nothing about
 * it can be imported from a generated module. What the bundle needs is small:
 * the marker attribute the build stamps on each pointable auto site
 * (`scripts/lib/ui-auto-stamp.mjs`), the shape of a site id, and the digest of
 * the auto tier the stamps were cut from, defined at build time by that
 * plugin. An empty digest (a test, a dev server with no stamp manifest) means
 * this bundle carries no auto sites: every auto guide is refused.
 */
export const UI_AUTO_ATTR = 'data-ui-auto'

export const UI_AUTO_BUILD_DIGEST: string =
  typeof __UI_AUTO_BUILD_DIGEST__ === 'string' ? __UI_AUTO_BUILD_DIGEST__ : ''

/** Same shape as `AUTO_SITE_ID_RE` in scripts/lib/ui-index.mjs. */
const AUTO_SITE_ID_RE = /^auto:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?::[0-9]+)?$/

/** Whether *id* is an auto render-site id (not a location id, and not a curated id). */
export function isAutoSiteId(id: string): boolean {
  return AUTO_SITE_ID_RE.test(id)
}

/** Whether *id* is an auto LOCATION id (`auto:<parent>:<label key>`), the id find_ui and ui.show name. */
export function isAutoLocationId(id: string): boolean {
  return /^auto:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$/.test(id)
}
