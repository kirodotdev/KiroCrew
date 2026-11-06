/**
 * Demo-build network layer: answers the dashboard's gateway traffic in the page
 * so the static demo runs with no gateway behind it.
 *
 * Installed by `demo/boot.ts` BEFORE the real entry module is imported, so every
 * module that captures `fetch` or constructs a socket at import time already sees
 * the replacements. Only same-origin `/api/...` traffic is answered here; static
 * assets (chunks, fonts, `index.html`, `build-id.json`) still load from the host.
 */
import { appPathname } from '../lib/basePath'
import { resolveRoute, type DemoRequest } from './routes'
import { FakeDashboardSocket } from './fakeSocket'

const JSON_HEADERS = { 'Content-Type': 'application/json' }

/** Paths the demo answered with a generic fallback, for widening the fixtures. */
const misses = new Set<string>()

function recordMiss(method: string, path: string): void {
  const key = `${method} ${path}`
  if (misses.has(key)) return
  misses.add(key)
  // The demo's one diagnostic: which calls fell through to the generic answer,
  // so a broken surface can be traced to the route it needs.
  // eslint-disable-next-line no-console
  console.debug('[demo] unhandled', key)
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase()
  if (typeof Request !== 'undefined' && input instanceof Request) return input.method.toUpperCase()
  return 'GET'
}

async function requestBody(input: RequestInfo | URL, init?: RequestInit): Promise<unknown> {
  let raw: string | null = null
  if (typeof init?.body === 'string') raw = init.body
  else if (typeof Request !== 'undefined' && input instanceof Request) {
    try { raw = await input.clone().text() } catch { raw = null }
  }
  if (!raw) return undefined
  try { return JSON.parse(raw) } catch { return raw }
}

function toUrl(input: RequestInfo | URL): URL | null {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  try { return new URL(raw, window.location.href) } catch { return null }
}

/** True for a request the demo answers instead of the network. */
function isGatewayRequest(url: URL): boolean {
  if (url.origin !== window.location.origin) return false
  const path = appPathname(url.pathname)
  return path.startsWith('/api/') || path === '/api' || path.startsWith('/app-assets/')
}

export function installDemoTransport(win: Window = window): void {
  const g = win as Window & typeof globalThis
  const realFetch = g.fetch.bind(g)

  g.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = toUrl(input)
    if (!url || !isGatewayRequest(url)) return realFetch(input, init)
    const req: DemoRequest = {
      method: requestMethod(input, init),
      path: appPathname(url.pathname),
      query: url.searchParams,
      body: await requestBody(input, init),
    }
    const res = resolveRoute(req)
    if (res.miss) recordMiss(req.method, req.path)
    if (res.delayMs) await new Promise((r) => setTimeout(r, res.delayMs))
    if (res.body === null) return new Response(null, { status: res.status })
    const body = typeof res.body === 'string' ? res.body : JSON.stringify(res.body)
    return new Response(body, { status: res.status, headers: res.headers ?? JSON_HEADERS })
  }

  // No beacon or push endpoint exists behind a static host.
  if (g.navigator && typeof g.navigator.sendBeacon === 'function') g.navigator.sendBeacon = () => true

  if (typeof g.EventSource === 'function') {
    // Server-sent streams never open: an EventSource that stays CONNECTING and
    // never errors, so no caller enters a reconnect loop.
    const Inert = function (this: EventSource, url: string | URL) {
      const es = new EventTarget() as unknown as EventSource & Record<string, unknown>
      Object.assign(es, { url: String(url), readyState: 0, withCredentials: false, close: () => undefined, onopen: null, onmessage: null, onerror: null })
      return es
    } as unknown as typeof EventSource
    Object.assign(Inert, { CONNECTING: 0, OPEN: 1, CLOSED: 2 })
    g.EventSource = Inert
  }

  if (typeof g.WebSocket === 'function') {
    const Real = g.WebSocket
    const Demo = function (this: WebSocket, url: string | URL) {
      let path = ''
      try { path = appPathname(new URL(String(url), g.location.href).pathname) } catch { /* foreign */ }
      return new FakeDashboardSocket(String(url), path === '/api/ws') as unknown as WebSocket
    } as unknown as typeof WebSocket
    Demo.prototype = Real.prototype
    Object.assign(Demo, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 })
    g.WebSocket = Demo
  }

  ;(g as unknown as { __demoMisses: () => string[] }).__demoMisses = () => [...misses].sort()
}
