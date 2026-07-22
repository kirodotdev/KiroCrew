// Single source of truth for the same-origin Tailwind v4 browser runtime asset.
//
// Consumed by three sites that MUST agree, or the runtime silently 404s and
// every widget/artifact renders unstyled (would regress):
//   - src/lib/widgetSrcdoc.ts : the <script src> the sandboxed iframe loads
//   - vite.config.ts (dev)    : configureServer middleware SERVE_PATH
//   - vite.config.ts (build)  : generateBundle emit fileName
// Keeping one definition here means the consumer path can't drift from the
// dev-serve / build-emit paths.

/** Public URL path the dashboard serves the Tailwind v4 runtime from (leading
 * slash). The sandboxed widget iframe is null-origin, so widgetSrcdoc prefixes
 * this with window.location.origin (a bare path won't resolve there). */
export const TAILWIND_RUNTIME_PATH = '/vendor/tailwindcss-browser.js'

/** Build-time source of the runtime, copied from the tracked @tailwindcss/browser
 * npm dependency (NOT a committed blob — supply-chain guidance). vite.config.ts only. */
export const TAILWIND_RUNTIME_SRC = './node_modules/@tailwindcss/browser/dist/index.global.js'
