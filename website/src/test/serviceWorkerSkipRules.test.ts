import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

// The service worker is plain JS in public/, never bundled, so nothing else in
// the suite ever executes it. That is exactly why its skip rules need a test:
// a wrong rule here is invisible in every dev loop and only shows up as a widget
// frame full of dashboard, or a document that 404s on a retry nobody asked for.
//
// This runs the REAL file rather than grepping it, so a rule that is present but
// unreachable (added below the respondWith, or after an early return) still fails.

// __dirname is src/test, so two levels up is the website root; every other test
// in this folder resolves repo files the same way.
const SW_PATH = path.resolve(__dirname, '..', '..', 'public', 'sw.js')
const ORIGIN = 'https://dash.example'

/** Register the worker's listeners against a fake global and return them. */
function loadWorker(): Record<string, (e: unknown) => void> {
  const listeners: Record<string, (e: unknown) => void> = {}
  const fakeSelf = {
    addEventListener: (kind: string, fn: (e: unknown) => void) => { listeners[kind] = fn },
    location: { origin: ORIGIN },
    skipWaiting: () => {},
    clients: { claim: () => {} },
  }
  const fakeCaches = {
    open: async () => ({ addAll: async () => {} }),
    keys: async () => [] as string[],
    match: async () => undefined,
    delete: async () => {},
  }
  // The shell path calls fetch(...).catch(...), so the stub must be thenable.
   
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(
    fakeSelf, fakeCaches, () => Promise.resolve({}),
  )
  return listeners
}

/** True when the worker took the request over instead of leaving it to the browser. */
function intercepts(path: string, mode: RequestMode = 'navigate'): boolean {
  const listeners = loadWorker()
  const respondWith = vi.fn()
  listeners.fetch({
    request: { method: 'GET', url: ORIGIN + path, mode },
    respondWith,
  })
  return respondWith.mock.calls.length > 0
}

describe('service worker skip rules', () => {
  it('never intercepts a single-use sandboxed document', () => {
    // Two distinct failures ride on this. The URL carries a one-shot credential
    // the gateway spends on the first GET, so any re-fetch the worker performs
    // resolves to a 404 and the frame shows an error page. And an iframe
    // navigation has mode 'navigate', so the offline fallback would serve the
    // SPA shell INTO the widget frame — a dashboard rendered inside a widget.
    expect(intercepts('/sandbox-doc/abc123/1700000000.mac')).toBe(false)
  })

  it('still owns the SPA shell, which is the reason it exists', () => {
    // The mirror of the assertion above: if this ever goes false the worker has
    // stopped doing its job and the skip test would pass vacuously.
    expect(intercepts('/')).toBe(true)
    expect(intercepts('/artifacts')).toBe(true)
  })

  it('leaves the API, app backends and hashed assets to the browser', () => {
    expect(intercepts('/api/artifacts')).toBe(false)
    expect(intercepts('/apps/dev-fleet/api/state')).toBe(false)
    expect(intercepts('/assets/App-abc123.js', 'no-cors')).toBe(false)
  })
})

// `mode === 'navigate'` is true for EVERY top-level document, not just the SPA.
// Keying the shell refresh on the mode alone stored an app-window document under
// `/` and `/index.html`, after which an offline dashboard navigation booted that
// document instead of the dashboard. Runs the real file, like the tests above.
function shellPutsFor(path: string): string[] {
  const puts: string[] = []
  const listeners: Record<string, (e: unknown) => void> = {}
  const fakeSelf = {
    addEventListener: (kind: string, fn: (e: unknown) => void) => { listeners[kind] = fn },
    location: { origin: ORIGIN },
    skipWaiting: () => {},
    clients: { claim: () => {} },
  }
  const fakeCaches = {
    open: async () => ({
      addAll: async () => {},
      put: async (key: string) => { puts.push(key) },
    }),
    keys: async () => [] as string[],
    match: async () => undefined,
    delete: async () => {},
  }
  const resp = { ok: true, clone: () => resp }
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(
    fakeSelf, fakeCaches, () => Promise.resolve(resp),
  )
  const waits: unknown[] = []
  listeners.fetch({
    request: { method: 'GET', url: ORIGIN + path, mode: 'navigate' },
    respondWith: (p: unknown) => { waits.push(p) },
    waitUntil: (p: unknown) => { waits.push(p) },
  })
  return { puts, settled: Promise.all(waits.map((p) => Promise.resolve(p).catch(() => {}))) } as unknown as string[]
}

describe('the shell cache refresh is scoped to the shell', () => {
  it('caches the SPA shell on a shell navigation', async () => {
    const r = shellPutsFor('/') as unknown as { puts: string[]; settled: Promise<unknown> }
    await r.settled
    expect(r.puts).toEqual(['/', '/index.html'])
  })

  it('does NOT overwrite the shell from a standalone app-window document', async () => {
    const r = shellPutsFor('/app-windows/mochi/panel.html') as unknown as { puts: string[]; settled: Promise<unknown> }
    await r.settled
    expect(r.puts).toEqual([])
  })
})
