import { afterEach } from 'vitest'
import '@testing-library/jest-dom'
import { server } from './mocks/server'
import { initI18n, i18next } from '../src/i18n'

// Initialize i18n for EVERY test file, pinned to English.
//
// Load-bearing: ~4000 existing assertions match visible English text
// (`getByText('Settings')`). Because `en.json` values are byte-identical to the
// literals they replaced (asserted by `englishIdentity.test.ts`), pinning 'en'
// here keeps every one of those assertions valid with no per-test setup — so a
// test that DOES go red signals a real extraction bug, not churn.
//
// Pinned explicitly rather than auto-detected: happy-dom reports the host's
// locale, which would make the suite pass or fail depending on the developer's
// machine language.
initI18n('en')

// Reset the language after every test. i18next is a module-level SINGLETON and
// `initI18n` is a no-op once initialized, so a test that switches language (or
// mounts a LanguageProvider with a non-English stored choice) leaves i18next on
// that language for every LATER test in the same file — turning ~4000 English
// text assertions into order-dependent failures. Caught exactly that way: an
// English-expecting test rendered Chinese because a preceding test had switched.
afterEach(() => {
  if (i18next.language !== 'en') void i18next.changeLanguage('en')
})

// happy-dom (unlike jsdom) performs REAL network I/O for DOM-driven loads: a
// widget's `<script src=".../tailwindcss-browser.js">` and a live `<iframe>`'s
// blob-page navigation are fetched over Node's http/https on DOM insertion.
// Under test that dials localhost:<port> — ECONNREFUSED spam AND an unclean
// libuv socket teardown that crashes the vitest fork worker
// (ERR_IPC_CHANNEL_CLOSED). jsdom never navigated iframes or eager-loaded
// scripts, so this whole class of dials is new to happy-dom.
//
// The neutralization lives in the msw layer, NOT in happy-dom internals: msw
// (msw/node) patches the same Node http/https happy-dom's Fetch uses, so the
// catch-all fallback handler in ./mocks/server.ts answers every otherwise
// -unmatched request (the vendor script, the blob iframe page) with an empty
// 200 before any real dial. This is instance-independent and version-robust —
// it needs no reach into happy-dom's `lib/*` modules (whose identity is
// unstable under Vite's transform) and cannot break msw's own interceptor.

// jsdom polyfill: PointerEvent. Radix UI (@radix-ui/react-dropdown-menu,
// @radix-ui/react-context-menu) checks `event instanceof PointerEvent` and
// ignores events that aren't pointer events. jsdom doesn't implement
// PointerEvent, so menus never open without this stub.
if (typeof window !== 'undefined' && !window.PointerEvent) {
  class Polyfill extends MouseEvent {
    readonly pointerId: number
    readonly pointerType: string
    constructor(type: string, params: PointerEventInit = {}) {
      super(type, params)
      this.pointerId = params.pointerId ?? 0
      this.pointerType = params.pointerType ?? ''
    }
  }
  ;(window as unknown as { PointerEvent: unknown }).PointerEvent = Polyfill
  ;(globalThis as unknown as { PointerEvent: unknown }).PointerEvent = Polyfill
}

// jsdom polyfill: Element.prototype.scrollIntoView (used by Radix focus management)
if (typeof window !== 'undefined') {
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView || function () {}
}

// jsdom polyfill: HTMLElement.prototype.hasPointerCapture / setPointerCapture /
// releasePointerCapture (used by Radix DismissableLayer)
if (typeof window !== 'undefined' && !HTMLElement.prototype.hasPointerCapture) {
  HTMLElement.prototype.hasPointerCapture = function () { return false }
  HTMLElement.prototype.setPointerCapture = function () {}
  HTMLElement.prototype.releasePointerCapture = function () {}
}

// Storage polyfill: on Node 22+ the runtime ships a native `localStorage`
// (gated by --localstorage-file) that vitest exposes globally; on Node 25 it
// shadows jsdom's spec-complete Storage with an incomplete one missing
// `.clear()`/`.key()`, breaking every test that does `localStorage.clear()` in
// beforeEach. Install a deterministic in-memory Storage so tests never depend
// on the Node/jsdom version's storage quirks.
// Methods live on a shared prototype so tests can spy on
// `Storage.prototype.setItem` (e.g. to simulate quota errors) and have it
// affect the polyfilled instances, matching real DOM Storage semantics.
const _StoragePrototype = (typeof Storage !== 'undefined' && Storage.prototype) || ({} as any)
_StoragePrototype.clear = function (this: any): void { this._m.clear() }
_StoragePrototype.getItem = function (this: any, k: string): string | null {
  return this._m.has(k) ? this._m.get(k) : null
}
_StoragePrototype.key = function (this: any, i: number): string | null {
  return Array.from(this._m.keys())[i] ?? null
}
_StoragePrototype.removeItem = function (this: any, k: string): void { this._m.delete(k) }
_StoragePrototype.setItem = function (this: any, k: string, v: string): void { this._m.set(k, String(v)) }
function _makeStorage(): Storage {
  const inst: any = Object.create(_StoragePrototype)
  inst._m = new Map<string, string>()
  Object.defineProperty(inst, 'length', { get() { return this._m.size }, configurable: true })
  return inst as Storage
}
for (const prop of ['localStorage', 'sessionStorage'] as const) {
  const store = _makeStorage()
  for (const target of [globalThis, typeof window !== 'undefined' ? window : undefined]) {
    if (target) {
      try {
        Object.defineProperty(target, prop, { value: store, writable: true, configurable: true })
      } catch {
        // ignore — some targets lock the property; the global definition wins
      }
    }
  }
}

// jsdom polyfill: window.matchMedia (used by useTheme → useSessionPalette)
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// jsdom polyfill: window.confirm / window.alert / window.prompt. happy-dom does
// not implement these dialog primitives (they are `undefined`). Tests that
// exercise a confirm-gated action spy on them
// (`vi.spyOn(window, 'confirm').mockReturnValue(true)`). Under vitest 3
// `vi.spyOn` tolerated an absent property, but vitest 4 THROWS
// ("can only spy on a function. Received undefined"), so every such test broke
// on the vitest 3→4 bump. Install spec-shaped no-op defaults (confirm/prompt
// deny by default, matching a user who dismisses the dialog) so spying works
// and un-spied calls stay deterministic instead of throwing.
if (typeof window !== 'undefined') {
  if (typeof window.confirm !== 'function') {
    ;(window as unknown as { confirm: (m?: string) => boolean }).confirm = () => false
  }
  if (typeof window.alert !== 'function') {
    ;(window as unknown as { alert: (m?: string) => void }).alert = () => {}
  }
  if (typeof window.prompt !== 'function') {
    ;(window as unknown as { prompt: (m?: string, d?: string) => string | null }).prompt = () => null
  }
}

// IntersectionObserver stub. Used by:
//   - WidgetFrame (lazy-load gate — needs to fire so `visible` flips true
//     and srcdoc/iframe gets built; otherwise theme/srcdoc tests inspect
//     an empty wrapper)
//   - usePaginatedMessages (top-of-list sentinel for load-more —
//     immediate-fire is safe because the hook guards on
//     visibleItems.length >= allItems.length and short-circuits when
//     there's nothing more to load)
// Fires synchronously on `observe()` with isIntersecting: true so both
// behaviours work in the same tests. Installed UNCONDITIONALLY (not guarded on
// absence): happy-dom SHIPS an IntersectionObserver, but its native one never
// fires in a no-layout test env, so `visible` would never flip. The
// synchronous-firing stub must REPLACE it, not defer to it.
if (typeof window !== 'undefined') {
  class StubIntersectionObserver {
    private readonly cb: IntersectionObserverCallback
    constructor(cb: IntersectionObserverCallback) { this.cb = cb }
    observe(target: Element) {
      // Fire once with isIntersecting=true. WidgetFrame disconnects after
      // the first hit; usePaginatedMessages re-arms the same target which
      // is fine — the load-more guard prevents runaway calls.
      const entry = {
        isIntersecting: true,
        target,
        intersectionRatio: 1,
        boundingClientRect: target.getBoundingClientRect(),
        intersectionRect: target.getBoundingClientRect(),
        rootBounds: null,
        time: 0,
      } as unknown as IntersectionObserverEntry
      this.cb([entry], this as unknown as IntersectionObserver)
    }
    unobserve() {}
    disconnect() {}
    takeRecords(): IntersectionObserverEntry[] { return [] }
  }
  ;(window as unknown as { IntersectionObserver: unknown }).IntersectionObserver = StubIntersectionObserver
  ;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = StubIntersectionObserver
}

// jsdom polyfill: ResizeObserver. Used by ChatPage (the sidebar collapse
// border-box morph measures the container height) and other layout-aware
// components. jsdom has no layout, so a no-op stub is sufficient — observers
// simply never fire.
if (typeof (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver === 'undefined') {
  class StubResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = StubResizeObserver
  if (typeof window !== 'undefined') {
    ;(window as unknown as { ResizeObserver: unknown }).ResizeObserver = StubResizeObserver
  }
}

// jsdom polyfill: EventSource. jsdom doesn't implement the SSE Web API; the
// useFileWatch / useLogSSE / useSSE hooks open an EventSource. Provide a
// minimal no-op stub so any component that opens a stream doesn't crash under
// test. Tests that need to drive SSE events install their own richer per-test
// mock, which overrides this stub.
if (typeof (globalThis as unknown as { EventSource?: unknown }).EventSource === 'undefined') {
  class StubEventSource {
    static readonly CONNECTING = 0
    static readonly OPEN = 1
    static readonly CLOSED = 2
    url: string
    readyState = 0
    onmessage: ((ev: MessageEvent) => void) | null = null
    onerror: ((ev: Event) => void) | null = null
    onopen: ((ev: Event) => void) | null = null
    constructor(url: string) { this.url = url }
    close() {}
    addEventListener() {}
    removeEventListener() {}
    dispatchEvent() { return false }
  }
  ;(globalThis as unknown as { EventSource: unknown }).EventSource = StubEventSource
}

// WebSocket stub. useWebSocket opens `ws://<host>/api/ws` on mount. jsdom has no
// WebSocket so the connect was a silent no-op under test; happy-dom SHIPS a
// native WebSocket that opens a REAL TCP socket to localhost, producing
// ECONNREFUSED spam and — worse — an unclean libuv socket teardown that crashes
// the fork worker (ERR_IPC_CHANNEL_CLOSED) at pool shutdown. Install a no-op
// stub UNCONDITIONALLY (replacing happy-dom's native class) so no test dials a
// real socket. Tests that drive WS events install their own richer per-test mock
// via vi.stubGlobal('WebSocket', …) / vi.mock('../hooks/useWebSocket'), which
// overrides this.
if (typeof window !== 'undefined') {
  class StubWebSocket {
    static readonly CONNECTING = 0
    static readonly OPEN = 1
    static readonly CLOSING = 2
    static readonly CLOSED = 3
    readonly CONNECTING = 0
    readonly OPEN = 1
    readonly CLOSING = 2
    readonly CLOSED = 3
    url: string
    readyState = 0
    onopen: ((ev: Event) => void) | null = null
    onmessage: ((ev: MessageEvent) => void) | null = null
    onerror: ((ev: Event) => void) | null = null
    onclose: ((ev: CloseEvent) => void) | null = null
    constructor(url: string) { this.url = url }
    send() {}
    close() { this.readyState = 3 }
    addEventListener() {}
    removeEventListener() {}
    dispatchEvent() { return false }
  }
  ;(window as unknown as { WebSocket: unknown }).WebSocket = StubWebSocket
  ;(globalThis as unknown as { WebSocket: unknown }).WebSocket = StubWebSocket
}

// jsdom polyfill: HTMLCanvasElement.getContext (used by scene canvases)
// jsdom doesn't implement canvas — mock getContext to return a no-op 2d context
const _origGetContext = HTMLCanvasElement.prototype.getContext
HTMLCanvasElement.prototype.getContext = function (type: string) {
  if (type === '2d') {
    const noop = () => {}
    const store: Record<string, any> = {}
    return new Proxy(store, {
      get: (_t, p) => (p in store ? store[p] : noop),
      set: (_t, p, v) => { store[p as string] = v; return true },
    }) as any
  }
  return _origGetContext.call(this, type) as any
}

// jsdom polyfill: WebGL2RenderingContext / WebGLRenderingContext. sigma (used by
// MemoryGraphTab) references `WebGL2RenderingContext` at module top-level, so any
// test that transitively imports sigma (App, routes, etc.) throws
// "WebGL2RenderingContext is not defined" at import time under jsdom, which has
// no WebGL. A bare stub class is enough — sigma is itself mocked in the
// MemoryGraphTab test, so the stub only needs to exist for the import to resolve.
if (typeof (globalThis as unknown as { WebGL2RenderingContext?: unknown }).WebGL2RenderingContext === 'undefined') {
  class StubWebGL2RenderingContext {}
  ;(globalThis as unknown as { WebGL2RenderingContext: unknown }).WebGL2RenderingContext = StubWebGL2RenderingContext
  if (typeof (globalThis as unknown as { WebGLRenderingContext?: unknown }).WebGLRenderingContext === 'undefined') {
    ;(globalThis as unknown as { WebGLRenderingContext: unknown }).WebGLRenderingContext = class {}
  }
}

// jsdom polyfill: URL.createObjectURL / revokeObjectURL. jsdom doesn't
// implement object URLs, but ArtifactBodyIframe builds a Blob URL on mount and
// revokes it on unmount. Without these stubs the iframe body throws
// "URL.createObjectURL is not a function" — an uncaught commit-phase error that
// only surfaces under some orderings (e.g. --coverage sharding on the fleet).
if (typeof URL.createObjectURL !== 'function') {
  URL.createObjectURL = (() => 'blob:mock') as typeof URL.createObjectURL
}
if (typeof URL.revokeObjectURL !== 'function') {
  URL.revokeObjectURL = (() => undefined) as typeof URL.revokeObjectURL
}

// Start MSW server before all tests
beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))

// Reset handlers after each test to prevent test pollution
afterEach(() => server.resetHandlers())

// Remove the session-expired banner api/client.ts appends to document.body on a
// 403 — RTL doesn't clean up body-appended nodes, so it lingers and steals focus
// from later tests. Don't click its dismiss button: that resets the module guard
// and the banner resurfaces mid-userEvent.type elsewhere (broke a cron table test).
afterEach(() => {
  document.getElementById('mc-session-expired')?.remove()
})

// Clean up after all tests are done
afterAll(() => server.close())
