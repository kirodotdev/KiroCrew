// A worker script is served with a short-lived cache policy (not the year-long
// ``immutable`` of an ordinary hashed chunk) because a same-origin worker takes
// its CSP from its OWN cached response header, so a header-only build must reach
// it promptly. See ``_asset_cache_control`` in
// ``src/kiro_crew/dashboard/server.py`` for the server side.
//
// The short-lived policy only governs a FRESH fetch, though. A browser that
// holds a worker in cache under the year-long ``immutable`` policy keeps that
// entry at the identical content-hashed URL and, per ``immutable`` semantics,
// never revalidates it, so a change to the served policy can never overwrite
// it. The worker URL must differ for the browser to leave the stranded
// immutable entry behind and fetch the worker afresh under the short-lived
// policy.
//
// ``__APP_VERSION__`` is the compile-time build identity (see ``define`` in
// ``vite.config.ts``). Appending it as a query string makes the worker URL a
// distinct cache key per version: it is part of the HTTP cache key, so each
// version requests a different URL than a browser holds under a stranded
// ``immutable`` entry, and that entry is left behind rather than replayed. It
// does not touch Vite's content hashing — the ``.js`` filename is unchanged,
// only a query suffix is added — so the worker still resolves to the same
// emitted chunk on disk.

/**
 * Append the build-identity cache key to a worker script URL so a browser
 * fetches this build's worker afresh instead of replaying a cache entry it
 * holds under a different build's cache policy.
 *
 * Accepts either a ``URL`` (from ``new URL('./worker.ts', import.meta.url)``)
 * or a string (from a ``?worker&url`` import) and returns a string usable as
 * the ``new Worker(...)`` argument.
 */
export function workerUrlWithBuildKey(url: URL | string): string {
  const base = String(url)
  // ``?v=`` for a bare URL, ``&v=`` to add a parameter to one that already has a
  // query. Written with the leading separator baked in so it is a request-path
  // contract fragment (matched by the ``^[?&][a-z_]+=$`` shape in
  // ``eslint.i18n.config.js``), not translatable copy — translating a query key
  // would break the fetch.
  const queryKey = base.includes('?') ? '&v=' : '?v='
  return `${base}${queryKey}${encodeURIComponent(__APP_VERSION__)}`
}
