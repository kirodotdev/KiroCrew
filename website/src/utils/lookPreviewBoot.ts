import { LIQUID_GLASS_STORAGE_KEY, applyLiquidGlass, readLiquidGlass } from './liquidGlass'
import { isLookPreviewFrame } from './lookPreview'
import { appPathname } from '../lib/basePath'

/**
 * Boot-time fences for the look-preview frame (utils/lookPreview.ts): the
 * scaled dashboard inside the first-run "Pick your look" step must never
 * change anything the parent document or the gateway owns.
 *
 * The frame is a full dashboard client on the parent's origin, so every write
 * its ordinary boot makes -- `gcOrphanedStorage` pruning "orphaned" session
 * keys against the demo slot list, `persistManualSentinels` reconciling the
 * unread reminders against it, `mc-active-slot-chat`, the saved tab set, the
 * theme boot's `mc-theme` write, `startUiPrefsSync`'s `PUT /api/ui-prefs` of
 * the frame's own storage, a popout `BroadcastChannel` post -- would land in
 * the user's real state. Rather than ask every writer to check
 * `isLookPreviewFrame()` (the next writer would forget), this module fences
 * the four things a write can travel through, at the platform level, BEFORE
 * any application module evaluates:
 *
 *  - `localStorage` and `sessionStorage` (same-origin frames share both) are
 *    swapped for read-through, write-local views: reads fall through to the
 *    real store, so the frame still sees the parent's `mc-theme` /
 *    `mc-color-theme` / `mc-liquid-glass`; writes and removals land in an
 *    in-memory overlay that shadows the real value for this document only. A
 *    `storage` event (the parent wrote a key) drops that key's overlay entry,
 *    so the parent's newest pick wins over anything the frame wrote itself
 *    (its theme boot, for one). That listener is registered here, first.
 *  - `window.fetch`: any request whose method is not GET or HEAD is answered
 *    locally with an empty JSON success and never sent, so the frame can read
 *    the gateway but not write to it -- `jfetch` callers and raw-`fetch`
 *    callers alike, today's and future ones. The same rule on
 *    `XMLHttpRequest`; `navigator.sendBeacon` sends nothing; `parent.postMessage`
 *    is a no-op; IndexedDB refuses to open and the Cache API is absent, so a
 *    writer that reaches for another shared store finds none.
 *  - `BroadcastChannel`: a channel the frame opens posts nothing (a post would
 *    reach the parent document directly).
 *  - `WebSocket`: only the dashboard's own `/api/ws` may open (kept so the
 *    frame never shows a disconnected state), and `send` is a no-op on it, so
 *    no read receipt, focus relay or subscription leaves; its frames are
 *    dropped unread in `useWebSocket`. Every other socket -- a docked
 *    terminal's, which the server would make the sole owner, displacing the
 *    parent's -- is an inert closed object that never dials.
 *
 * Imported FIRST by `main.tsx`: ES module evaluation follows import order, so
 * this runs before the extension root, the store, the API client or any hook
 * touches the platform. Outside the frame every function here does nothing.
 */

function fenceStorage(win: Window, name: 'localStorage' | 'sessionStorage'): boolean {
  let real: Storage
  try {
    real = win[name]
  } catch {
    return false // blocked store: nothing to protect, nothing to read through
  }
  const overlay = new Map<string, string | null>()
  const keys = () => {
    const seen = new Set<string>()
    for (let i = 0; i < real.length; i++) { const k = real.key(i); if (k !== null) seen.add(k) }
    for (const [k, v] of overlay) { if (v === null) seen.delete(k); else seen.add(k) }
    return [...seen]
  }
  const fence: Storage = {
    get length() { return keys().length },
    key(i: number) { return keys()[i] ?? null },
    getItem(k: string) {
      const o = overlay.get(k)
      if (o !== undefined) return o
      return real.getItem(k)
    },
    setItem(k: string, v: string) { overlay.set(k, String(v)) },
    removeItem(k: string) { overlay.set(k, null) },
    clear() { for (const k of keys()) overlay.set(k, null) },
  }
  Object.defineProperty(win, name, { configurable: true, get: () => fence })
  if (name === 'localStorage') {
    win.addEventListener('storage', e => {
      if (e.key === null) overlay.clear()
      else overlay.delete(e.key)
    })
  }
  return true
}

export function installLookPreviewStorageFence(win: Window = window): boolean {
  if (!isLookPreviewFrame()) return false
  const a = fenceStorage(win, 'localStorage')
  const b = fenceStorage(win, 'sessionStorage')
  return a || b
}

/** Methods the frame may send to the gateway: reads only. */
const READ_METHODS = new Set(['GET', 'HEAD'])

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase()
  if (typeof Request !== 'undefined' && input instanceof Request) return input.method.toUpperCase()
  return 'GET'
}

type Globals = Window & typeof globalThis

export function installLookPreviewNetworkFence(win: Window = window): boolean {
  if (!isLookPreviewFrame()) return false
  const g = win as Globals
  const realFetch = g.fetch.bind(g)
  g.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    if (READ_METHODS.has(requestMethod(input, init))) return realFetch(input, init)
    // An empty JSON object, so the `j` parsers that follow every write resolve
    // as they would on a real success and the caller's code path is unchanged.
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (typeof g.BroadcastChannel === 'function') {
    const Real = g.BroadcastChannel
    const Silent = function (this: BroadcastChannel, name: string) {
      const ch = new Real(name)
      ch.postMessage = () => undefined
      return ch
    } as unknown as typeof BroadcastChannel
    Silent.prototype = Real.prototype
    g.BroadcastChannel = Silent
  }
  // The other roads a write can take out of a document, closed the same way.
  // Every one here either reaches the gateway (beacon, XHR) or the parent
  // document (postMessage) or another shared store (IndexedDB, Cache).
  if (g.navigator && typeof g.navigator.sendBeacon === 'function') {
    g.navigator.sendBeacon = () => true
  }
  if (typeof g.XMLHttpRequest === 'function') {
    const proto = g.XMLHttpRequest.prototype
    const realOpen = proto.open
    proto.open = function (this: XMLHttpRequest & { __lpWrite?: boolean }, method: string, ...rest: unknown[]) {
      this.__lpWrite = !READ_METHODS.has(String(method).toUpperCase())
      return (realOpen as (...a: unknown[]) => void).call(this, method, ...rest)
    } as typeof proto.open
    const realSend = proto.send
    proto.send = function (this: XMLHttpRequest & { __lpWrite?: boolean }, body?: Document | XMLHttpRequestBodyInit | null) {
      if (this.__lpWrite) return
      return realSend.call(this, body as XMLHttpRequestBodyInit | null | undefined)
    }
  }
  if (g.parent && g.parent !== g) {
    // The parent is the step that embeds this frame; it listens to nothing from
    // the frame and must not be made to.
    g.parent.postMessage = () => undefined
  }
  if (g.indexedDB) {
    const refuse = () => { throw new DOMException('look-preview frame: shared stores are read-only', 'InvalidStateError') }
    g.indexedDB.open = refuse as typeof g.indexedDB.open
    g.indexedDB.deleteDatabase = refuse as typeof g.indexedDB.deleteDatabase
  }
  if (g.caches) {
    Object.defineProperty(g, 'caches', { configurable: true, get: () => undefined })
  }
  if (typeof g.WebSocket === 'function') {
    const Real = g.WebSocket
    Real.prototype.send = () => undefined
    // Only the dashboard's own socket may open (kept so the frame never shows a
    // disconnected state). Any other -- a docked terminal's
    // `/api/ws/terminal/<id>`, for one, where the server makes the newest
    // connection the sole owner and displaces the parent's -- is answered with
    // an inert, already-closed socket that never dials.
    const Fenced = function (this: WebSocket, url: string | URL, protocols?: string | string[]) {
      let path = ''
      try { path = new URL(String(url), g.location.href).pathname } catch { /* unparseable: treat as foreign */ }
      // Under a sub-path build the base-path shim (lib/installBasePath.ts, the
      // import right after this module) rebases the URL before it reaches this
      // constructor, so the dashboard socket arrives as `<base>/api/ws`.
      if (appPathname(path) === LOOK_PREVIEW_ALLOWED_SOCKET_PATH) return new Real(url, protocols)
      const inert = new EventTarget() as unknown as WebSocket & Record<string, unknown>
      Object.assign(inert, { readyState: Real.CLOSED, url: String(url), protocol: '', extensions: '', bufferedAmount: 0, binaryType: 'blob', send: () => undefined, close: () => undefined, onopen: null, onclose: null, onerror: null, onmessage: null })
      return inert
    } as unknown as typeof WebSocket
    Fenced.prototype = Real.prototype
    Object.assign(Fenced, { CONNECTING: Real.CONNECTING, OPEN: Real.OPEN, CLOSING: Real.CLOSING, CLOSED: Real.CLOSED })
    g.WebSocket = Fenced
  }
  return true
}

/** The one socket the frame may dial: the dashboard's own, `hooks/websocket/connection.ts`. */
export const LOOK_PREVIEW_ALLOWED_SOCKET_PATH = '/api/ws'

/**
 * The frame's half of the Translucent panels switch. No `useLiquidGlass` hook
 * mounts inside the frame (the Settings row and the first-run row are the only
 * owners, and neither renders there), so the root attribute index.css keys the
 * glass on would otherwise be whatever `index.html` wrote at boot and never
 * move again. Here the frame applies the parent's stored value and re-applies
 * it on every `storage` event for the key -- the event fires in the OTHER
 * same-origin documents, never the writer, which is exactly the frame.
 */
export function followParentLiquidGlass(win: Window = window): boolean {
  if (!isLookPreviewFrame()) return false
  applyLiquidGlass(readLiquidGlass())
  win.addEventListener('storage', e => {
    if (e.key === LIQUID_GLASS_STORAGE_KEY || e.key === null) applyLiquidGlass(readLiquidGlass())
  })
  return true
}

/**
 * Glass only reads as glass with something under it, and a transcript pinned
 * to its newest message clears the composer dock by design. So once the demo
 * transcript has rendered, the frame scrolls it up by a little under one dock
 * height -- a position the real page reaches every time the reader scrolls
 * back -- so the newest bubble sits under the dock and the frost shows. The
 * real page's own layout and clearance are untouched; this only moves the
 * scroll position of a picture nobody can scroll.
 */
export const LOOK_PREVIEW_SCROLL_BACK_PX = 150

export function scrollDemoTranscriptUnderDock(doc: Document = document, deadlineMs = 8000): boolean {
  if (!isLookPreviewFrame()) return false
  const started = Date.now()
  const tick = () => {
    const scroller = doc.querySelector<HTMLElement>('.chat-container')
    if (scroller && scroller.scrollHeight > scroller.clientHeight + LOOK_PREVIEW_SCROLL_BACK_PX) {
      scroller.scrollTop = scroller.scrollHeight - scroller.clientHeight - LOOK_PREVIEW_SCROLL_BACK_PX
      return
    }
    if (Date.now() - started < deadlineMs) setTimeout(tick, 150)
  }
  // Let the first paint land; the transcript hydrates a little after boot.
  setTimeout(tick, 400)
  return true
}

installLookPreviewStorageFence()
installLookPreviewNetworkFence()
followParentLiquidGlass()
scrollDemoTranscriptUnderDock()
