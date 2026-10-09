/**
 * Build-time base path for serving the dashboard under a URL sub-path.
 *
 * `KIROCREW_BASE_PATH` is read once, when Vite builds the bundle, and becomes
 * Vite's `base`. Nothing reads it at runtime: the gateway serves `index.html`
 * byte-for-byte as built, so a sub-path deployment is a separate build. Unset
 * (or `/`) builds the stock root-mounted dashboard.
 */

/** Characters allowed in one path segment of the base. */
const SEGMENT_RE = /^[A-Za-z0-9._~-]+$/

/**
 * Normalize a base path to Vite's form: a leading and a trailing `/`.
 * Returns `/` for unset, empty, or `/`.
 *
 * Throws on anything that is not a plain path: a scheme, a host (`//x`), a
 * query or fragment, or a `.`/`..` segment. The value lands in every asset URL
 * and in the router basename, so a malformed one must stop the build instead of
 * shipping a dashboard that cannot load its own scripts.
 *
 * @param {string | undefined | null} raw
 * @returns {string}
 */
export function resolveBuildBase(raw) {
  const value = (raw ?? '').trim()
  if (value === '' || value === '/') return '/'
  if (!value.startsWith('/') || value.startsWith('//')) {
    throw new Error(`KIROCREW_BASE_PATH must be an absolute path such as /proxy/kirocrew, got ${JSON.stringify(value)}`)
  }
  const segments = value.split('/').filter((s) => s !== '')
  for (const segment of segments) {
    if (segment === '.' || segment === '..' || !SEGMENT_RE.test(segment)) {
      throw new Error(`KIROCREW_BASE_PATH has an invalid segment ${JSON.stringify(segment)} in ${JSON.stringify(value)}`)
    }
  }
  return `/${segments.join('/')}/`
}

/** Bare specifier -> served path of its vendor stub, relative to the base. */
const VENDOR_IMPORTS = [
  ['react', 'vendor/react.mjs'],
  ['react-dom', 'vendor/react-dom.mjs'],
  ['react-dom/client', 'vendor/react-dom-client.mjs'],
  ['react/jsx-runtime', 'vendor/react-jsx-runtime.mjs'],
  ['@kirocrew/app-sdk', 'vendor/kirocrew-app-sdk.mjs'],
  ['@kirocrew/app-sdk/ui', 'vendor/kirocrew-ui.mjs'],
  ['@tanstack/react-query', 'vendor/tanstack-react-query.mjs'],
  ['lucide-react', 'vendor/lucide-react.mjs'],
]

/**
 * The import map the dashboard shell carries, with every stub under `base`.
 * Vite does not rewrite URLs inside an inline `<script type="importmap">`, so
 * the plugin that emits it builds the paths here.
 *
 * @param {string} base a value returned by {@link resolveBuildBase}
 * @returns {{ imports: Record<string, string> }}
 */
export function vendorImportMap(base) {
  return { imports: Object.fromEntries(VENDOR_IMPORTS.map(([spec, rel]) => [spec, `${base}${rel}`])) }
}

/** The shell's service-worker registration call, exactly as index.html writes it. */
export const SW_REGISTER_CALL = "navigator.serviceWorker.register('/sw.js')"

/**
 * The shell for a sub-path build: the service worker is not registered.
 *
 * public/sw.js routes by root paths (`/api`, `/assets/`, `/`), so under a base
 * it would cache API responses and serve the wrong shell. A sub-path build runs
 * without it (no offline shell) rather than with a worker that misroutes. The
 * stock build never calls this, so its shell is unchanged.
 *
 * Throws if the call is missing, so a reworded registration cannot silently
 * slip a misrouting worker into a sub-path build.
 *
 * @param {string} html
 * @returns {string}
 */
export function withoutServiceWorker(html) {
  if (!html.includes(SW_REGISTER_CALL)) {
    throw new Error(`index.html no longer contains ${SW_REGISTER_CALL}; update withoutServiceWorker in scripts/lib/basePath.mjs`)
  }
  return html.replace(SW_REGISTER_CALL, 'Promise.resolve()')
}
