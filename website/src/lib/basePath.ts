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
 * transport, and {@link installBasePathDomShims} does the same for URLs written
 * into the DOM, instead of at every call site. A dynamic `import()` and a
 * `location` navigation reach neither hook, so those call sites use
 * {@link withBase}; basePath.test.ts guards that.
 */

/** `import.meta.env.BASE_URL` without its trailing slash; `''` at the root. */
export const BASE_PATH: string = toBasePath(import.meta.env.BASE_URL)

/** Turn Vite's `BASE_URL` (`/` or `/a/b/`) into the `/a/b` form, `''` for root. */
export function toBasePath(baseUrl: string | undefined): string {
  const trimmed = (baseUrl ?? '/').replace(/\/+$/, '')
  return trimmed.startsWith('/') ? trimmed : ''
}

function hasBase(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`)
}

/**
 * `window.location.pathname` with the base removed, so a check like
 * `startsWith('/embed/')` reads the app route in a sub-path build too. Code
 * inside the router should use its `location`, which already has no base.
 */
export function appPathname(pathname: string = window.location.pathname, base: string = BASE_PATH): string {
  if (base === '' || !hasBase(pathname, base)) return pathname
  return pathname.slice(base.length) || '/'
}

/** The `basename` for the router: `undefined` at the root (the router default). */
export function routerBasename(base: string = BASE_PATH): string | undefined {
  return base === '' ? undefined : base
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

/** URL-valued attributes the app writes with root-relative paths. */
const urlAttributes = new Set(['src', 'href', 'poster', 'action'])

/** Element interface -> its URL-valued property, for code that sets `el.src = ...`. */
type Ctor = { prototype: object } | undefined
function urlProperties(w: Record<string, unknown>): ReadonlyArray<readonly [Ctor, string]> {
  const c = (x: unknown) => x as Ctor
  return [
    [c(w.HTMLImageElement), 'src'],
    [c(w.HTMLScriptElement), 'src'],
    [c(w.HTMLIFrameElement), 'src'],
    [c(w.HTMLSourceElement), 'src'],
    [c(w.HTMLMediaElement), 'src'],
    [c(w.HTMLVideoElement), 'poster'],
    [c(w.HTMLAnchorElement), 'href'],
    [c(w.HTMLLinkElement), 'href'],
  ]
}

type DomShimWindow = {
  location: { host: string }
  open?: (url?: string | URL, ...rest: string[]) => unknown
  Element: { prototype: Element }
} & Record<string, unknown>

/**
 * Route URLs the app writes into the DOM (`<img src="/api/...">`,
 * `<a href="/chat">`, `el.src = ...`, `window.open('/x')`) through the base, the
 * same way {@link installBasePathShims} does for transports. React writes these
 * attributes through `setAttribute`, so one hook covers every rendered element.
 * A no-op that returns `false` in the stock build.
 */
export function installBasePathDomShims(win: DomShimWindow = window as unknown as DomShimWindow, base: string = BASE_PATH): boolean {
  if (base === '') return false
  const rebase = (url: string) => rebaseUrl(url, win.location.host, base)
  const proto = win.Element.prototype
  const nativeSetAttribute = proto.setAttribute
  proto.setAttribute = function setAttribute(this: Element, name: string, value: string) {
    const next = urlAttributes.has(name.toLowerCase()) ? rebase(String(value)) : value
    return nativeSetAttribute.call(this, name, next)
  }
  for (const [ctor, prop] of urlProperties(win)) {
    const desc = ctor && Object.getOwnPropertyDescriptor(ctor.prototype, prop)
    if (!ctor || !desc?.set || !desc.configurable) continue
    const nativeSet = desc.set
    Object.defineProperty(ctor.prototype, prop, {
      ...desc,
      set(this: Element, value: string) { nativeSet.call(this, rebase(String(value))) },
    })
  }
  // `new Audio(url)` sets `src` inside the constructor, past the setter hook.
  const NativeAudio = win.Audio as (abstract new (...args: never[]) => unknown) | undefined
  if (typeof NativeAudio === 'function') win.Audio = wrapConstructor(NativeAudio, rebase)
  if (typeof win.open === 'function') {
    const nativeOpen = win.open.bind(win)
    win.open = (url?: string | URL, ...rest: string[]) =>
      nativeOpen(url == null ? url : rebase(String(url)), ...rest)
  }
  return true
}
