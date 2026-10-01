/**
 * The relocatable dashboard runtime (R1-A).
 *
 * One typed seam for every URL the dashboard aims at ITS OWN gateway: HTTP API
 * calls, WebSocket constructors, the React Router basename, same-dashboard
 * navigation, and (through `relocateLoose`) the shared `api/client.ts` request
 * helpers. It exists so the same unmodified SPA bundle can run in two shapes:
 *
 *  - `direct`: the dashboard is served at the origin root (desktop app, plain
 *    `http://localhost:<port>`). Every resolver here is the IDENTITY on the
 *    pre-seam URL — byte-for-byte what the code produced before this module.
 *  - `relayed-pane`: the dashboard is served by the hub under a short-lived,
 *    capability-prefixed, SAME-ORIGIN path (`/instance-pane/<capability>/…`) so
 *    a remote Crew's dashboard can be embedded through one published HTTPS
 *    origin without a wildcard host. Only the PATH gains a prefix; the origin,
 *    protocol and host are the hub's own and are read live.
 *
 * This is the frontend half of the capability relay described in
 * `.audit/pane-load-kc-46d84a/architecture-synthesis.md` (candidate B base). It
 * deliberately does NOT monkey-patch `fetch`/`WebSocket`/DOM URL setters and
 * does NOT rewrite the built bundle: gateway-bound call sites take the base from
 * this one typed module. The repository guard
 * (`scripts/check-dashboard-runtime.mjs`) fails a new root-literal gateway URL
 * that bypasses it.
 *
 * Production resolves to `direct` whenever the dashboard is served at the origin
 * root (the desktop app, plain `http://localhost:<port>`, and every non-relay
 * load); it resolves to `relayed-pane` when the hub serves this document under
 * the capability prefix, which the backend relay
 * (`src/kiro_crew/dashboard/instance_pane_relay.py`) now does for an embedded
 * Remote Crew pane. Both branches are live and exercised end to end.
 */

/**
 * A same-dashboard, root-relative gateway path. The template-literal type means
 * a literal like `'/api/ws'` (or a template `` `/api/ws/terminal/${id}` ``) is
 * accepted with no cast, while an absolute URL (`'https://…'`) or a scheme
 * (`'blob:…'`) is a compile error — external targets can never be routed through
 * the relay by mistake.
 */
export type GatewayPath = `/${string}`

/**
 * How the dashboard is being served, and therefore how its own URLs resolve.
 * A discriminated union so an impossible mix (a relay base with no capability,
 * a direct mode carrying a prefix) cannot be represented.
 */
export type DashboardRuntime =
  | { readonly kind: 'direct'; readonly basePath: '/'; readonly routerBasename: '/' }
  | {
      readonly kind: 'relayed-pane'
      /** Always ends in a slash: `/instance-pane/<capability>/`. */
      readonly basePath: `/instance-pane/${string}/`
      /** The `basePath` without its trailing slash — React Router's basename shape. */
      readonly routerBasename: `/instance-pane/${string}`
      /** Pane relay protocol. Pinned to 1; a future incompatible shape fails closed. */
      readonly protocol: 1
    }

const DIRECT_RUNTIME: DashboardRuntime = Object.freeze({
  kind: 'direct',
  basePath: '/',
  routerBasename: '/',
})

/**
 * The reserved relay prefix. A capability is one non-empty URL-safe segment
 * (the hub mints a base64url-style token), and a trailing slash MUST follow it,
 * so the base is unambiguous. Anything else — an empty capability, a missing
 * trailing segment, a look-alike prefix — is not a pane document.
 */
const RELAY_PREFIX_RE = /^\/instance-pane\/([A-Za-z0-9_-]+)\//

/**
 * Resolve the runtime from a location's pathname. Pure and total: an
 * unrecognized shape fails closed to `direct` rather than guessing a base.
 */
export function resolveDashboardRuntime(loc: Pick<Location, 'pathname'>): DashboardRuntime {
  const match = RELAY_PREFIX_RE.exec(loc.pathname)
  if (match === null) return DIRECT_RUNTIME
  const capability = match[1]
  return Object.freeze({
    kind: 'relayed-pane',
    basePath: `/instance-pane/${capability}/`,
    routerBasename: `/instance-pane/${capability}`,
    protocol: 1,
  })
}

/** Resolve a typed gateway path to the string a request/socket should target. */
export function httpPath(runtime: DashboardRuntime, path: GatewayPath): string {
  switch (runtime.kind) {
    case 'direct':
      return path
    case 'relayed-pane':
      return runtime.basePath + path.slice(1)
    default: {
      const _exhaustive: never = runtime
      return _exhaustive
    }
  }
}

/** Build the WebSocket URL for a gateway path. Origin/protocol are read live. */
export function webSocketUrl(
  runtime: DashboardRuntime,
  loc: Pick<Location, 'protocol' | 'host'>,
  path: GatewayPath,
): string {
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${loc.host}${httpPath(runtime, path)}`
}

/** The React Router basename: `'/'` in direct mode, the capability prefix in relay mode. */
export function routerBasename(runtime: DashboardRuntime): string {
  switch (runtime.kind) {
    case 'direct':
      return runtime.routerBasename
    case 'relayed-pane':
      return runtime.routerBasename
    default: {
      const _exhaustive: never = runtime
      return _exhaustive
    }
  }
}

/**
 * Relocate an arbitrary request URL string — the resolver `api/client.ts`'s
 * blessed helpers adopt. In `direct` mode it is the IDENTITY function, so every
 * migrated helper is byte-for-byte unchanged there. In `relayed-pane` mode it
 * prefixes ONLY root-absolute, same-dashboard paths; protocol-relative (`//…`),
 * scheme (`https:`, `blob:`, `data:`), and document-relative URLs pass through
 * untouched, and an already-relocated path is never prefixed twice.
 */
export function relocateLoose(runtime: DashboardRuntime, url: string): string {
  if (runtime.kind === 'direct') return url
  if (!url.startsWith('/')) return url
  if (url.startsWith('//')) return url
  if (url.startsWith(runtime.basePath)) return url
  return runtime.basePath + url.slice(1)
}

// ── Stateful convenience layer ──────────────────────────────────────────────
// One module singleton, resolved once before app startup (main.tsx calls
// `initDashboardRuntime()` first) and frozen. Module-scope readers that capture
// a URL at import time therefore see a stable base. `currentDashboardRuntime`
// lazily resolves from `window.location` if init has not run (tests, and any
// import-order edge), so a caller never observes a null.

let _runtime: DashboardRuntime | null = null

/** Resolve and freeze the runtime once, before the app renders. Idempotent. */
export function initDashboardRuntime(loc: Pick<Location, 'pathname'> = window.location): DashboardRuntime {
  if (_runtime === null) _runtime = resolveDashboardRuntime(loc)
  return _runtime
}

/** The frozen runtime singleton, lazily resolved from `window.location` if needed. */
export function currentDashboardRuntime(): DashboardRuntime {
  if (_runtime === null) _runtime = resolveDashboardRuntime(window.location)
  return _runtime
}

/** The string a same-dashboard HTTP request should target. */
export function dashboardHttpPath(path: GatewayPath): string {
  return httpPath(currentDashboardRuntime(), path)
}

/**
 * `fetch` for a typed gateway path. New callers use this; existing bare-`fetch`
 * callers relocate their URL through `relocateRequestUrl` instead. In relay mode
 * ambient credentials are omitted (the opaque pane must not attach the hub's
 * cookies); direct mode keeps `fetch`'s default so behavior is unchanged.
 */
export function dashboardFetch(path: GatewayPath, init?: RequestInit): Promise<Response> {
  const runtime = currentDashboardRuntime()
  const target = httpPath(runtime, path)
  if (runtime.kind === 'relayed-pane') {
    return fetch(target, { credentials: 'omit', ...init })
  }
  return fetch(target, init)
}

/** Construct a WebSocket aimed at a same-dashboard gateway path. */
export function dashboardWebSocket(path: GatewayPath, protocols?: string | string[]): WebSocket {
  const url = webSocketUrl(currentDashboardRuntime(), window.location, path)
  return protocols === undefined ? new WebSocket(url) : new WebSocket(url, protocols)
}

/** The React Router basename for the current runtime. */
export function dashboardRouterBasename(): string {
  return routerBasename(currentDashboardRuntime())
}

/**
 * Relocate an arbitrary request URL string against the current runtime — the
 * entry `api/client.ts`'s helpers call. Identity in direct mode.
 */
export function relocateRequestUrl(url: string): string {
  return relocateLoose(currentDashboardRuntime(), url)
}

/**
 * The URL a full-page same-dashboard navigation (`location.assign`/`href`)
 * should target, so a relayed pane navigates within its own capability prefix
 * instead of escaping to the hub root. Identity in direct mode.
 */
export function dashboardNavigateUrl(path: GatewayPath): string {
  return httpPath(currentDashboardRuntime(), path)
}
