/**
 * First path segments under `/api/apps/<app>/` that CORE owns rather than the app.
 *
 * Textual mirror of `CORE_APP_ROUTE_SEGMENTS` in `apps/manifest.py`, which carries the
 * enumeration, the source files it was read from, and the rule that a new core route
 * mounted under `/api/apps/{name}/` is added to BOTH sides in the same commit. Being
 * inside the app's own namespace is not sufficient: core mounts the lifecycle routes
 * there, so a contributed row naming one would POST to a core handler with the
 * reader's own session, on a row they clicked believing it belonged to the app.
 *
 * ## Why this is its own module
 *
 * These are URL path segments, not copy, and `eslint.i18n.config.js` releases this
 * FILE so editing the set does not fail the zero-tolerance `[added-lines]` i18n gate.
 * A global `words.exclude` shape cannot do that job here: the values are bare
 * lowercase words (`open`, `update`, `config`), so a whole-value-anchored global
 * exemption would also release a button whose label is exactly "Open". Keeping them
 * apart from `fileMenuContributions.tsx` — which DOES render copy — is what makes the
 * file-scoped release honest. Keep this module route segments only; anything with a
 * user-visible string belongs elsewhere.
 */
export const CORE_APP_ROUTE_SEGMENTS = new Set([
  '_jobs',
  'config',
  'dev',
  'disable',
  'enable',
  'manifest',
  'migrate-cleanup',
  'open',
  'token',
  'uninstall',
  'update',
])

/**
 * App names that are NOT an app's own namespace, whatever an app is called.
 *
 * Textual mirror of `RESERVED_APP_PATH_SEGMENTS` in `apps/manifest.py`. These are shared
 * literal routes registered before the `/api/apps/{name}` catch-all
 * (`/api/apps/registries/refresh`, `/api/apps/registry/install`), and the segment BELOW
 * the prefix is not a per-app lifecycle name, so `CORE_APP_ROUTE_SEGMENTS` does not cover
 * them. Reserving the name at install is forward-looking only and leaves an
 * already-published app so named, so the endpoint check is what has to refuse it.
 */
export const RESERVED_APP_PATH_SEGMENTS = new Set([
  'blob',
  'install',
  'register',
  'registries',
  'registry',
])
