/**
 * The dashboard's side of an app frame's network, for a frame loaded from `srcdoc`.
 *
 * A sandboxed frame has an opaque origin, so its own requests carry no cookie. With
 * an authenticating reverse proxy in front of the dashboard, the proxy answers those
 * requests with its login page, and the frame never loads its module or reaches its
 * backend. A srcdoc frame (`sandboxFrame.loadMintedFrame`) instead asks this page to
 * make the request: the page sends exactly the path, method, headers and body the
 * frame gave, with the page's normal cookies and nothing else added. The gateway's
 * own checks (the capability on `/app-frame-fetch/`, the app's grant) run on that
 * request as they would on the frame's, and refuse what they refuse today.
 *
 * What a frame may ask for is narrower than a URL: its own app's fetch routes
 * (`/app-frame-fetch/<app>/...`, the methods that route accepts) and its own app's
 * UI files (`/apps/<app>/ui/...`, GET), with only the request headers a cross-origin
 * request from the frame could send without failing its preflight. The answer
 * carries what a CORS read would have exposed to the frame: the CORS-safelisted
 * response headers, and for a fetch route only a response the gateway marked
 * readable by the frame (`Access-Control-Allow-Origin: null`, which it sets only
 * once the capability verified); anything else is a network error, as it is today.
 *
 * A message counts only from the frame's own window and with that frame's relay
 * nonce, minted here and sent to it in `init`.
 *
 * @module appFrame/frameRelay
 */
import { withBase } from '../lib/basePath'
import { APP_FRAME_TAG } from './frameProtocol'

/** Methods `/app-frame-fetch/` accepts (`_FETCH_METHODS` in `apps/app_frame.py`). */
export const RELAY_FETCH_METHODS: ReadonlySet<string> = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
/** Request headers the frame may send: what the fetch route's preflight allows
 *  (`_CORS_ALLOW_HEADERS`), plus the CORS-safelisted ones a request never preflights. */
export const RELAY_REQUEST_HEADERS: ReadonlySet<string> = new Set([
  'content-type',
  'x-kirocrew-capability',
  'accept',
  'accept-language',
  'content-language',
])
/** Response headers the frame may read: the CORS-safelisted set. */
export const RELAY_RESPONSE_HEADERS: ReadonlySet<string> = new Set([
  'cache-control',
  'content-language',
  'content-length',
  'content-type',
  'expires',
  'last-modified',
  'pragma',
])
/** Largest request body a frame may hand over, in bytes. */
export const MAX_RELAY_BODY_BYTES = 8 * 1024 * 1024
/** Requests one frame may have in flight at once. */
export const MAX_RELAY_IN_FLIGHT = 32

export interface RelayRequest {
  path: string
  method: string
  headers: [string, string][]
  body: string | ArrayBuffer | null
}

/**
 * Where a frame of `appName` may send `path` with `method`, as a path under the
 * dashboard's mount; null when it is not one of that app's own routes.
 */
export function relayTarget(appName: string, path: unknown, method: unknown): string | null {
  if (typeof path !== 'string' || typeof method !== 'string') return null
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\') || path.length > 4096) return null
  // Dot segments (literal or percent-encoded) never name another route.
  if (/(^|\/)(\.|%2e){1,2}(\/|$|\?)/i.test(path)) return null
  // No percent-encoded slash, backslash or dot in the path: a front proxy that
  // decodes them would resolve `..%2f` into another route after this check.
  const rawPath = path.split('?', 1)[0]
  if (/%(2f|5c|2e)/i.test(rawPath)) return null
  let url: URL
  try {
    url = new URL(path, 'https://dashboard.invalid')
  } catch {
    return null
  }
  if (url.origin !== 'https://dashboard.invalid' || url.hash) return null
  const app = encodeURIComponent(appName)
  if (!app || app !== appName) return null
  const target = url.pathname + url.search
  // Decoded once, the path must still be the same app's and hold no dot segment.
  let decoded: string
  try {
    decoded = decodeURIComponent(url.pathname)
  } catch {
    return null
  }
  if (/(^|\/)\.{1,2}(\/|$)/.test(decoded) || decoded.includes('\\')) return null
  if (url.pathname.startsWith(`/app-frame-fetch/${app}/`)) return RELAY_FETCH_METHODS.has(method) ? target : null
  if (url.pathname.startsWith(`/apps/${app}/ui/`)) return method === 'GET' ? target : null
  return null
}

/** The request headers to send, or null when the frame named one it could not send. */
export function relayHeaders(raw: unknown): Headers | null {
  if (!Array.isArray(raw)) return null
  const headers = new Headers()
  for (const pair of raw) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== 'string' || typeof pair[1] !== 'string') return null
    const name = pair[0].toLowerCase()
    if (!RELAY_REQUEST_HEADERS.has(name)) return null
    headers.set(name, pair[1])
  }
  return headers
}

function bodyBytes(body: unknown): number {
  if (body === null || body === undefined) return 0
  // The UTF-8 bytes the request will carry; a UTF-16 unit is at most 3 of them,
  // so only a long string is encoded to count.
  if (typeof body === 'string') return body.length * 3 <= MAX_RELAY_BODY_BYTES ? body.length : new TextEncoder().encode(body).byteLength
  if (body instanceof ArrayBuffer) return body.byteLength
  return Infinity
}

interface RelayState {
  el: HTMLIFrameElement
  appName: string
  nonce: string
  inFlight: Map<number, AbortController>
  /** Called once when the frame leaves the document. */
  onGone?: () => void
}

const relays = new Map<Window, RelayState>()
let listening = false
/** How often a live relay checks that its frame is still in the document. A frame
 *  removed with a request still streaming must not keep that request open. */
export const RELAY_SWEEP_MS = 1000
let sweeper: number | null = null

function post(state: RelayState, msg: Record<string, unknown>, transfer: Transferable[] = []): void {
  // An opaque origin cannot be named as a target, hence '*'. The window is this
  // frame's own, and the answer is to its own request.
  // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
  try { state.el.contentWindow?.postMessage({ [APP_FRAME_TAG]: 1, ...msg }, '*', transfer) } catch { /* frame gone */ }
}

/** Drop relays whose frame has left the document. */
function sweep(): void {
  for (const [win, state] of relays) {
    if (state.el.isConnected) continue
    for (const ac of state.inFlight.values()) ac.abort()
    relays.delete(win)
    try { state.onGone?.() } catch { /* the owner's own */ }
  }
  if (relays.size === 0 && listening) {
    window.removeEventListener('message', onMessage)
    listening = false
  }
  if (relays.size === 0 && sweeper !== null) {
    window.clearInterval(sweeper)
    sweeper = null
  }
}

async function answer(state: RelayState, id: number, target: string, req: RelayRequest, headers: Headers): Promise<void> {
  const ac = new AbortController()
  state.inFlight.set(id, ac)
  const fetchRoute = target.startsWith('/app-frame-fetch/')
  let started = false
  try {
    const init: RequestInit = {
      method: req.method,
      headers,
      // The page's own cookies, as any request this page makes: same-origin only.
      credentials: 'same-origin',
      mode: 'same-origin',
      redirect: 'error',
      signal: ac.signal,
    }
    if (req.body !== null && req.method !== 'GET') init.body = req.body
    const res = await fetch(withBase(target), init)
    // What a CORS read from the frame would have seen: on a fetch route, only a
    // response the gateway marked readable by the opaque origin.
    if (fetchRoute && res.headers.get('access-control-allow-origin') !== 'null') {
      post(state, { type: 'relay.response', id, ok: false })
      return
    }
    const exposed: [string, string][] = []
    res.headers.forEach((v, k) => { if (RELAY_RESPONSE_HEADERS.has(k.toLowerCase())) exposed.push([k, v]) })
    // Status and headers at once, then the body as it arrives: a long-lived
    // response (an event stream) reaches the frame chunk by chunk.
    post(state, { type: 'relay.response', id, ok: true, status: res.status, statusText: res.statusText, headers: exposed })
    started = true
    const reader = res.body?.getReader()
    if (reader) {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        const chunk = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength)
        post(state, { type: 'relay.chunk', id, chunk }, [chunk])
      }
    }
    post(state, { type: 'relay.end', id })
  } catch {
    // Before the answer started: a network error, as a refused read is. After: the
    // body broke off (or the frame aborted it).
    post(state, started ? { type: 'relay.error', id } : { type: 'relay.response', id, ok: false })
  } finally {
    state.inFlight.delete(id)
  }
}

function onMessage(e: MessageEvent): void {
  sweep()
  const state = e.source ? relays.get(e.source as Window) : undefined
  if (!state) return
  const m = e.data as Record<string, unknown> | null
  if (!m || typeof m !== 'object' || m[APP_FRAME_TAG] !== 1) return
  if (m.type !== 'relay' && m.type !== 'relay.abort') return
  // The frame's own relay nonce, sent to it in `init`.
  if (m.nonce !== state.nonce) return
  const id = m.id
  if (typeof id !== 'number' || !Number.isInteger(id) || id <= 0) return
  if (m.type === 'relay.abort') {
    state.inFlight.get(id)?.abort()
    return
  }
  const refuse = () => post(state, { type: 'relay.response', id, ok: false })
  if (state.inFlight.has(id) || state.inFlight.size >= MAX_RELAY_IN_FLIGHT) { refuse(); return }
  const method = typeof m.method === 'string' ? m.method.toUpperCase() : ''
  const target = relayTarget(state.appName, m.path, method)
  const headers = relayHeaders(m.headers)
  const body = m.body ?? null
  if (!target || !headers || bodyBytes(body) > MAX_RELAY_BODY_BYTES) { refuse(); return }
  void answer(state, id, target, { path: target, method, headers: [], body: body as string | ArrayBuffer | null }, headers)
}

/** A fresh relay nonce. */
export function relayNonce(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('')
}

/**
 * Serve `el`'s relay requests for `appName`'s routes, under `nonce`. Ends when the
 * frame leaves the document.
 */
export function serveFrameRelay(el: HTMLIFrameElement, appName: string, nonce: string, onGone?: () => void): void {
  sweep()
  const win = el.contentWindow
  // No window: the frame is not in a document, so there is nothing to serve.
  if (!win) { onGone?.(); return }
  relays.set(win, { el, appName, nonce, inFlight: new Map(), onGone })
  if (!listening) {
    window.addEventListener('message', onMessage)
    listening = true
  }
  if (sweeper === null) sweeper = window.setInterval(sweep, RELAY_SWEEP_MS)
}

/** For tests: forget every relay. */
export function resetFrameRelaysForTest(): void {
  for (const state of relays.values()) for (const ac of state.inFlight.values()) ac.abort()
  relays.clear()
  if (listening) window.removeEventListener('message', onMessage)
  listening = false
  if (sweeper !== null) window.clearInterval(sweeper)
  sweeper = null
}
