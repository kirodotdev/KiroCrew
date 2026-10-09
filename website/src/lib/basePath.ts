/**
 * The dashboard's URL base, fixed when the bundle was built.
 *
 * A build with `KIROCREW_BASE_PATH=/proxy/kirocrew` (see
 * website/scripts/lib/basePath.mjs) sets Vite's `base`, which Vite exposes here
 * as `import.meta.env.BASE_URL`. The stock build has `BASE_URL === '/'`, so
 * `BASE_PATH` is `''` and every helper below is the identity.
 *
 * The app calls the gateway with root-relative URLs (`fetch('/api/...')`,
 * `new WebSocket(`${proto}//${host}/api/ws`)`). Under a sub-path those escape
 * the mount, so {@link installBasePathShims} prefixes them once, at the
 * transport, instead of at every call site.
 */

/** `import.meta.env.BASE_URL` without its trailing slash; `''` at the root. */
export const BASE_PATH: string = toBasePath(import.meta.env.BASE_URL)

/** Turn Vite's `BASE_URL` (`/` or `/a/b/`) into the `/a/b` form, `''` for root. */
export function toBasePath(baseUrl: string | undefined): string {
  const trimmed = (baseUrl ?? '/').replace(/\/+$/, '')
  return trimmed.startsWith('/') ? trimmed : ''
}

/** The `basename` for the router: `undefined` at the root (the router default). */
export function routerBasename(base: string = BASE_PATH): string | undefined {
  return base === '' ? undefined : base
}

function hasBase(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`)
}

/**
 * Prefix a root-relative path (`/chat`, `/api/x?y`) with the base. Anything
 * else (relative, protocol-relative, absolute URL) and a path already under the
 * base is returned unchanged.
 */
export function withBase(path: string, base: string = BASE_PATH): string {
  if (base === '' || !path.startsWith('/') || path.startsWith('//')) return path
  const end = path.search(/[?#]/)
  const pathname = end === -1 ? path : path.slice(0, end)
  return hasBase(pathname, base) ? path : `${base}${path}`
}

/**
 * {@link withBase} for anything a transport accepts: a root-relative path, or
 * an absolute http(s)/ws(s) URL on this page's own host. Other hosts are left
 * alone, so a call to a third-party origin is never rewritten.
 */
export function rebaseUrl(url: string, host: string, base: string = BASE_PATH): string {
  if (base === '') return url
  if (url.startsWith('/')) return withBase(url, base)
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return url
  }
  if (!/^(https?|wss?):$/.test(parsed.protocol) || parsed.host !== host) return url
  if (hasBase(parsed.pathname, base)) return url
  parsed.pathname = `${base}${parsed.pathname}`
  return parsed.toString()
}

type ShimWindow = Pick<typeof globalThis, 'fetch' | 'WebSocket' | 'EventSource' | 'Request'> & {
  location: { host: string }
}

function wrapConstructor<T extends abstract new (...args: never[]) => unknown>(
  ctor: T,
  rebase: (url: string) => string,
): T {
  return new Proxy(ctor, {
    construct(target, args: unknown[], newTarget) {
      const [first, ...rest] = args
      const url = typeof first === 'string' || first instanceof URL ? rebase(String(first)) : first
      return Reflect.construct(target, [url, ...rest], newTarget) as object
    },
  })
}

/**
 * Route `fetch`, `WebSocket` and `EventSource` through the base. A no-op that
 * returns `false` when the bundle was built for the root, so the stock build
 * keeps the browser's own transports untouched.
 */
export function installBasePathShims(win: ShimWindow = window, base: string = BASE_PATH): boolean {
  if (base === '') return false
  const rebase = (url: string) => rebaseUrl(url, win.location.host, base)
  const nativeFetch = win.fetch.bind(win)
  const NativeRequest = win.Request
  win.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input === 'string' || input instanceof URL) return nativeFetch(rebase(String(input)), init)
    if (input instanceof NativeRequest) {
      const next = rebase(input.url)
      return nativeFetch(next === input.url ? input : new NativeRequest(next, input), init)
    }
    return nativeFetch(input, init)
  }) as typeof fetch
  if (win.WebSocket) win.WebSocket = wrapConstructor(win.WebSocket, rebase)
  if (win.EventSource) win.EventSource = wrapConstructor(win.EventSource, rebase)
  return true
}
