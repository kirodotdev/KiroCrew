import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

// The installed app's share target sends a GET navigation to `/share?text=...`.
// The gateway's request-line limit is about 8 KB, so a long share that goes to
// the network comes back as a bare 400 page and the share is lost. The worker
// answers that navigation from the cached shell instead, and the page reads
// `location.search` itself. These tests run the REAL sw.js, as the skip-rule
// tests beside this one do.

const SW_PATH = path.resolve(__dirname, '..', '..', 'public', 'sw.js')
const ORIGIN = 'https://dash.example'

type Listener = (e: unknown) => void

/** The worker loaded against a fake global. `cached` is what the shell cache holds, if anything. */
function loadWorker(cached: { body: string } | undefined) {
  const listeners: Record<string, Listener> = {}
  const fakeSelf = {
    addEventListener: (kind: string, fn: Listener) => { listeners[kind] = fn },
    location: { origin: ORIGIN },
    skipWaiting: () => {},
    clients: { claim: () => {} },
  }
  const fakeCaches = {
    open: async () => ({ addAll: async () => {}, put: async () => {} }),
    keys: async () => [] as string[],
    match: async (key: string) => (key === '/index.html' ? cached : undefined),
    delete: async () => {},
  }
  const network = vi.fn(() => Promise.resolve({ ok: true, body: 'network', clone() { return this } }))
  new Function('self', 'caches', 'fetch', readFileSync(SW_PATH, 'utf8'))(fakeSelf, fakeCaches, network)
  return { listeners, network }
}

/** What the worker answers a request with, or `undefined` when it leaves the request to the browser. */
async function answer(cached: { body: string } | undefined, requestPath: string, mode: RequestMode = 'navigate') {
  const { listeners, network } = loadWorker(cached)
  const respondWith = vi.fn()
  listeners.fetch({
    request: { method: 'GET', url: ORIGIN + requestPath, mode },
    respondWith,
    waitUntil: () => {},
  })
  const response = respondWith.mock.calls.length ? await respondWith.mock.calls[0][0] : undefined
  return { response, network }
}

const SHELL = { body: 'cached shell' }

describe('service worker share target', () => {
  it('answers a long share from the cached shell with no network request', async () => {
    const long = '/share?text=' + encodeURIComponent('x'.repeat(20000))
    const { response, network } = await answer(SHELL, long)
    expect(response).toBe(SHELL)
    expect(network).not.toHaveBeenCalled()
  })

  it('answers a share with no query from the cached shell too', async () => {
    const { response, network } = await answer(SHELL, '/share')
    expect(response).toBe(SHELL)
    expect(network).not.toHaveBeenCalled()
  })

  it('goes to the network when no shell is cached, as before', async () => {
    const { response, network } = await answer(undefined, '/share?text=hi')
    expect(response).toMatchObject({ body: 'network' })
    expect(network).toHaveBeenCalledTimes(1)
  })

  it('keeps every other page network-first', async () => {
    for (const page of ['/sharer', '/share/extra', '/chat?new=1&prefill=hi']) {
      const { response, network } = await answer(SHELL, page)
      expect(response).toMatchObject({ body: 'network' })
      expect(network).toHaveBeenCalledTimes(1)
    }
  })

  it('leaves a sign-in link to the gateway even on the share path', async () => {
    const { response, network } = await answer(SHELL, '/share?token=abc&text=hi')
    expect(response).toBeUndefined()
    expect(network).not.toHaveBeenCalled()
  })

  it('does not take over a sub-resource request for the share path', async () => {
    const { response } = await answer(SHELL, '/share?text=hi', 'cors')
    expect(response).toMatchObject({ body: 'network' })
  })
})
