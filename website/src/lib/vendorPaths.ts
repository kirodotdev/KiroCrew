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

/** Public URL path the dashboard serves the Mermaid browser bundle from.
 *
 * Same three-site contract as the Tailwind runtime above (consumer, dev-serve
 * middleware, build emit), and same reason it exists: the Meetings sketch artist
 * renders into a null-origin sandboxed iframe whose CSP is `connect-src 'none'`,
 * so it cannot fetch Mermaid from a public CDN. Serving it from our own origin
 * is what lets that frame draw a diagram with NO network egress at all — the
 * app works fully offline and a prompt-injected document has no exfiltration
 * channel. Consumed by src/apps/meetings/lib/sketchSrcdoc.ts. */
export const MERMAID_RUNTIME_PATH = '/vendor/mermaid.min.js'

/** Build-time source of the Mermaid bundle, copied from the tracked `mermaid`
 * npm dependency (already a direct dependency for chat markdown rendering, so
 * this adds no new supply-chain surface). vite.config.ts only. */
export const MERMAID_RUNTIME_SRC = './node_modules/mermaid/dist/mermaid.min.js'
