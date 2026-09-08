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

  it('leaves the API and app backends to the browser', () => {
    expect(intercepts('/api/artifacts')).toBe(false)
    expect(intercepts('/apps/dev-fleet/api/state')).toBe(false)
  })

  it('DOES take hashed assets over, so a 5xx can be retried', () => {
    // Not for caching — the immutable HTTP cache still owns them. The worker sits
    // in the path only so that one 502 on a module script does not kill the page.
    // The shell above is served from cache, so a module graph that fails leaves a
    // dark skeleton that no reload clears.
    expect(intercepts('/assets/App-abc123.js', 'no-cors')).toBe(true)
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

// A page load asks for the whole module graph at once, and the hop in front of the
// gateway has a lower real concurrency ceiling than it advertises: `tailscale
// serve` announces 250 concurrent HTTP/2 streams and starts answering 502 past
// roughly 140. Measured against a Windows gateway on one connection — 120 streams:
// all 120 OK; 200 streams: 140 OK + 60x502; 247 streams: 143 OK + 104x502. A 502 on
// a module script is not a degraded page, it is a dead one, and the shell keeps
// coming from the cache above so no reload escapes it. Hence the retry, and hence
// these tests: the SW is the only layer that runs when the app cannot boot, so
// staleShellHeal (a boot-time probe) can never reach this failure.
/** Drive one asset request with a scripted fetch, and report the attempts made. */
function assetRequest(steps: Array<number | 'throw'>): {
  result: Promise<{ status?: number }>
  attempts: () => number
} {
  let calls = 0
  const listeners: Record<string, (e: unknown) => void> = {}
  const fakeSelf = {
    addEventListener: (kind: string, fn: (e: unknown) => void) => { listeners[kind] = fn },
    location: { origin: ORIGIN },
    skipWaiting: () => {},
    clients: { claim: () => {} },
  }
  const fakeCaches = {
    open: async () => ({ addAll: async () => {}, put: async () => {} }),
    keys: async () => [] as string[],
    match: async () => undefined,
    delete: async () => {},
  }
  // Past the end of the script the LAST step repeats, so a one-element script
  // means "always this".
  const fetchStub = () => {
    const step = steps[Math.min(calls, steps.length - 1)]
    calls += 1
    return step === 'throw'
      ? Promise.reject(new TypeError('network error'))
      : Promise.resolve({ status: step })
  }
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(
    fakeSelf, fakeCaches, fetchStub,
  )
  let captured: Promise<{ status?: number }> = Promise.resolve({})
  listeners.fetch({
    request: { method: 'GET', url: ORIGIN + '/assets/App-abc123.js', mode: 'no-cors' },
    respondWith: (p: Promise<{ status?: number }>) => { captured = p },
  })
  return { result: captured, attempts: () => calls }
}

describe('hashed-asset 5xx retry', () => {
  it('passes a first-try 200 straight through, with no second attempt', async () => {
    const r = assetRequest([200])
    expect((await r.result).status).toBe(200)
    expect(r.attempts()).toBe(1)
  })

  it('recovers a 502 that succeeds on retry — the black-screen case', async () => {
    const r = assetRequest([502, 200])
    expect((await r.result).status).toBe(200)
    expect(r.attempts()).toBe(2)
  })

  it('does NOT retry a 404, which is a genuinely missing asset', async () => {
    // A stale shell pointing at a pruned build must surface at once instead of
    // costing three round trips per missing module.
    const r = assetRequest([404])
    expect((await r.result).status).toBe(404)
    expect(r.attempts()).toBe(1)
  })

  it('gives up after a bounded number of attempts and reports the real status', async () => {
    // Bounded, so a genuinely broken gateway is not hammered; and the LAST real
    // response is handed back rather than a synthetic network error, so the
    // browser's console names the actual failure.
    const r = assetRequest([503])
    expect((await r.result).status).toBe(503)
    expect(r.attempts()).toBe(3)
  })

  it('retries a thrown network error too, and still resolves', async () => {
    const r = assetRequest(['throw', 200])
    expect((await r.result).status).toBe(200)
    expect(r.attempts()).toBe(2)
  })

  it('keeps a captured 5xx when a later attempt throws', async () => {
    // The give-up branch promises the browser the TRUE status. A dropped
    // connection on a later attempt must not downgrade an already-seen 502 into a
    // synthetic network error — during the burst this fix is for, a 5xx and a
    // dropped connection arrive together, so this ordering is the common one.
    const r = assetRequest([502, 'throw'])
    expect((await r.result).status).toBe(502)
    expect(r.attempts()).toBe(3)
  })
})
