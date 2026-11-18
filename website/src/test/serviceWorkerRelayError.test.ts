import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

// The Kiro relay answers a navigation to a Crew whose host is gone with a 503,
// its typed `x-kiro-relay-error` header and a JSON body. Runs the real worker
// file, like serviceWorkerSkipRules.test.ts.
const SW_PATH = path.resolve(__dirname, '..', '..', 'public', 'sw.js')
const ORIGIN = 'https://crew.relay.example'
const SHELL = { ok: true, status: 200, body: 'shell' }

type FakeResponse = { ok: boolean; status: number; body: string; headers?: { get: (k: string) => string | null }; clone?: () => FakeResponse }

function relayAnswer(status: number, reason: string | null): FakeResponse {
  const headers = { get: (k: string) => (k.toLowerCase() === 'x-kiro-relay-error' ? reason : null) }
  const r: FakeResponse = { ok: status >= 200 && status < 300, status, body: '{"reason":"crew_not_connected"}', headers }
  r.clone = () => r
  return r
}

/** What the worker answers a navigation to `pathname` with, given the network's answer. */
async function navigate(pathname: string, network: FakeResponse, cached: FakeResponse | null = SHELL): Promise<FakeResponse> {
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
    match: async (key: unknown) => (key === '/index.html' ? cached ?? undefined : undefined),
    delete: async () => {},
  }
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(
    fakeSelf, fakeCaches, () => Promise.resolve(network),
  )
  let answer: Promise<FakeResponse> | undefined
  listeners.fetch({
    request: { method: 'GET', url: ORIGIN + pathname, mode: 'navigate' },
    respondWith: (p: Promise<FakeResponse>) => { answer = p },
    waitUntil: () => {},
  })
  if (!answer) throw new Error('the worker left the navigation to the browser')
  return answer
}

describe('service worker: a relay error answer to a navigation', () => {
  it('boots the cached shell instead of the relay body', async () => {
    expect(await navigate('/chat', relayAnswer(503, 'crew_not_connected'))).toBe(SHELL)
  })

  it('keeps the relay answer when no shell is cached yet', async () => {
    const network = relayAnswer(503, 'crew_not_connected')
    expect(await navigate('/', network, null)).toBe(network)
  })

  it("leaves the Crew's own error pages and every success alone", async () => {
    const own = relayAnswer(404, null)
    expect(await navigate('/missing', own)).toBe(own)
    const ok = relayAnswer(200, null)
    expect(await navigate('/chat', ok)).toBe(ok)
  })
})
