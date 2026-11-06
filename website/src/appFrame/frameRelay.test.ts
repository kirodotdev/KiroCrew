import { describe, it, expect, afterEach, beforeAll, vi } from 'vitest'
import { APP_FRAME_TAG } from './frameProtocol'
import {
  MAX_RELAY_IN_FLIGHT,
  RELAY_SWEEP_MS,
  relayHeaders,
  relayTarget,
  resetFrameRelaysForTest,
  serveFrameRelay,
} from './frameRelay'
import { BOOT_GRACE_MS, loadMintedFrame, SRCDOC_SELF } from './sandboxFrame'
import { installFrameWindows, postFrom, sentTo } from '../test/LinkPreviewFramesTestUtils'

beforeAll(() => installFrameWindows())
afterEach(() => {
  resetFrameRelaysForTest()
  vi.unstubAllGlobals()
  document.body.replaceChildren()
})

const flush = () => new Promise((r) => setTimeout(r, 0))

function frame(): HTMLIFrameElement {
  const el = document.createElement('iframe')
  document.body.append(el)
  return el
}

function relayFrom(el: HTMLIFrameElement | Window, data: Record<string, unknown>): void {
  postFrom({ [APP_FRAME_TAG]: 1, type: 'relay', nonce: 'n1', id: 1, method: 'GET', headers: [], body: null, ...data },
    el instanceof HTMLIFrameElement ? el.contentWindow : el)
}

const answers = (el: HTMLIFrameElement) => sentTo(el).filter((m) => m.type === 'relay.response')

describe('relayTarget: a frame reaches its own app and nothing else', () => {
  it.each([
    ['/app-frame-fetch/cards/kv', 'GET', '/app-frame-fetch/cards/kv'],
    ['/app-frame-fetch/cards/kv?x=1', 'POST', '/app-frame-fetch/cards/kv?x=1'],
    ['/apps/cards/ui/dist/link-preview.mjs', 'GET', '/apps/cards/ui/dist/link-preview.mjs'],
  ])('%s %s is the app own route', (path, method, want) => {
    expect(relayTarget('cards', path, method)).toBe(want)
  })

  it.each([
    ['another app fetch route', '/app-frame-fetch/other/kv', 'GET'],
    ['another app UI file', '/apps/other/ui/x.mjs', 'GET'],
    ['a core API route', '/api/apps/cards/config', 'GET'],
    ['the dashboard API', '/api/status', 'GET'],
    ['a frame document', '/app-frame/cards/linkPreviews/cards/f/t', 'GET'],
    ['a dot segment out of the app', '/app-frame-fetch/cards/../other/kv', 'GET'],
    ['an encoded dot segment', '/apps/cards/ui/%2e%2e/%2e%2e/api/status', 'GET'],
    ['dots before an encoded slash', '/apps/cards/ui/x/..%2f..%2f..%2f..%2fapi/status', 'GET'],
    ['an encoded slash', '/app-frame-fetch/cards/a%2F..%2F..%2Fother/kv', 'GET'],
    ['an encoded backslash', '/apps/cards/ui/x%5c..%5capi', 'GET'],
    ['an encoded dot alone', '/apps/cards/ui/%2E/x.mjs', 'GET'],
    ['a scheme-relative URL', '//evil.example/apps/cards/ui/x.mjs', 'GET'],
    ['an absolute URL', 'https://evil.example/app-frame-fetch/cards/kv', 'GET'],
    ['a backslash', '/app-frame-fetch/cards\\..\\other', 'GET'],
    ['a UI file written to', '/apps/cards/ui/x.mjs', 'POST'],
    ['a method the fetch route refuses', '/app-frame-fetch/cards/kv', 'OPTIONS'],
    ['a prefix that is not a segment', '/app-frame-fetch/cardser/kv', 'GET'],
  ])('%s is refused', (_why, path, method) => {
    expect(relayTarget('cards', path, method)).toBeNull()
  })

  it('only headers the frame could have sent cross-origin', () => {
    expect(relayHeaders([['Content-Type', 'application/json'], ['X-KiroCrew-Capability', 'c']])?.get('x-kirocrew-capability')).toBe('c')
    expect(relayHeaders([['authorization', 'Bearer x']])).toBeNull()
    expect(relayHeaders([['cookie', 'a=b']])).toBeNull()
    expect(relayHeaders([['x-internal-secret', 's']])).toBeNull()
    expect(relayHeaders('nope')).toBeNull()
  })
})

describe('serveFrameRelay: the dashboard makes the request the frame asked for', () => {
  it('sends exactly the path, method, headers and body, with the page cookies only', async () => {
    const fetch = vi.fn(async () => new Response('{"ok":1}', { status: 200, headers: { 'content-type': 'application/json', 'access-control-allow-origin': 'null', 'set-cookie': 'x=y', 'x-secret': 's' } }))
    vi.stubGlobal('fetch', fetch)
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    relayFrom(el, { path: '/app-frame-fetch/cards/kv?a=1', method: 'POST', headers: [['content-type', 'application/json'], ['x-kirocrew-capability', 'cap']], body: '{"k":1}' })
    await flush(); await flush()
    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/app-frame-fetch/cards/kv?a=1')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('same-origin')
    expect(init.mode).toBe('same-origin')
    expect(init.redirect).toBe('error')
    expect(init.body).toBe('{"k":1}')
    expect([...new Headers(init.headers)]).toEqual([['content-type', 'application/json'], ['x-kirocrew-capability', 'cap']])
    const [res] = answers(el)
    expect(res).toMatchObject({ id: 1, ok: true, status: 200 })
    // Only what a CORS read would expose.
    expect(res.headers).toEqual([['content-type', 'application/json']])
  })

  it('streams the body: the head goes at once, chunks follow while the response stays open', async () => {
    const enc = new TextEncoder()
    let push: ((s: string) => void) | null = null
    const body = new ReadableStream<Uint8Array>({ start(c) { push = (s) => c.enqueue(enc.encode(s)) } })
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn(async (_u: string, init: RequestInit) => {
      signal = init.signal ?? undefined
      return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream', 'access-control-allow-origin': 'null' } })
    }))
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    relayFrom(el, { path: '/app-frame-fetch/cards/events' })
    await flush(); await flush()
    expect(sentTo(el).map((m) => m.type)).toEqual(['relay.response'])
    push!('data: one\n\n')
    await flush(); await flush()
    const chunk = sentTo(el).find((m) => m.type === 'relay.chunk')
    expect(new TextDecoder().decode(chunk?.chunk as ArrayBuffer)).toBe('data: one\n\n')
    expect(sentTo(el).some((m) => m.type === 'relay.end')).toBe(false)
    // The frame cancels: the dashboard stops the request.
    relayFrom(el, { type: 'relay.abort' })
    expect(signal?.aborted).toBe(true)
  })

  it('a fetch-route answer the gateway did not mark readable is a network error, as today', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"code":"capability_invalid"}', { status: 401 })))
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    relayFrom(el, { path: '/app-frame-fetch/cards/kv' })
    await flush(); await flush()
    expect(answers(el)).toEqual([expect.objectContaining({ id: 1, ok: false })])
  })

  it('another app path is refused without a request', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    relayFrom(el, { path: '/app-frame-fetch/other/kv' })
    relayFrom(el, { id: 2, path: '/api/status' })
    await flush()
    expect(fetch).not.toHaveBeenCalled()
    expect(answers(el).map((m) => [m.id, m.ok])).toEqual([[1, false], [2, false]])
  })

  it('a message from another window, or with another nonce, is ignored', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const el = frame()
    const other = frame()
    serveFrameRelay(el, 'cards', 'n1')
    relayFrom(other, { path: '/apps/cards/ui/x.mjs' })
    relayFrom(el, { path: '/apps/cards/ui/x.mjs', nonce: 'guess' })
    relayFrom(window, { path: '/apps/cards/ui/x.mjs' })
    await flush()
    expect(fetch).not.toHaveBeenCalled()
    expect(answers(el)).toEqual([])
    expect(answers(other)).toEqual([])
  })

  it('in-flight requests are bounded', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    for (let id = 1; id <= MAX_RELAY_IN_FLIGHT + 1; id++) relayFrom(el, { id, path: '/apps/cards/ui/x.mjs' })
    await flush()
    expect(answers(el)).toEqual([expect.objectContaining({ id: MAX_RELAY_IN_FLIGHT + 1, ok: false })])
  })

  it('a frame removed mid-stream has its request aborted without another message', async () => {
    vi.useFakeTimers()
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((_u: string, init: RequestInit) => {
      signal = init.signal ?? undefined
      return new Promise(() => {})
    }))
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    relayFrom(el, { path: '/app-frame-fetch/cards/events' })
    await vi.advanceTimersByTimeAsync(0)
    expect(signal?.aborted).toBe(false)
    el.remove()
    await vi.advanceTimersByTimeAsync(RELAY_SWEEP_MS)
    expect(signal?.aborted).toBe(true)
    vi.useRealTimers()
  })

  it('a string body is measured in UTF-8 bytes, so one under the cap is sent', async () => {
    const fetch = vi.fn(async () => new Response('', { status: 200, headers: { 'access-control-allow-origin': 'null' } }))
    vi.stubGlobal('fetch', fetch)
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    // 3 MiB of ASCII: 3 MiB on the wire, under the 8 MiB cap.
    relayFrom(el, { path: '/app-frame-fetch/cards/kv', method: 'POST', body: 'a'.repeat(3 * 1024 * 1024) })
    await flush(); await flush()
    expect(fetch).toHaveBeenCalledTimes(1)
    // Over the cap in bytes (multi-byte characters) is still refused.
    relayFrom(el, { id: 2, path: '/app-frame-fetch/cards/kv', method: 'POST', body: '\u00e9'.repeat(4.5 * 1024 * 1024) })
    await flush()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(answers(el).find((m) => m.id === 2)).toMatchObject({ ok: false })
  })

  it('a frame that left the document is no longer served', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const el = frame()
    serveFrameRelay(el, 'cards', 'n1')
    el.remove()
    relayFrom(el, { path: '/apps/cards/ui/x.mjs' })
    await flush()
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('loadMintedFrame: by URL first, srcdoc only when the URL load is not the gateway document', () => {
  const MINTED = { frame: '/app-frame/cards/linkPreviews/cards/f/t', capability: 'cap', ttl: 900 }
  const DOC = `<meta http-equiv="Content-Security-Policy" content="script-src ${SRCDOC_SELF} 'nonce-N'; connect-src ${SRCDOC_SELF}">`
  const boot = (el: HTMLIFrameElement) => postFrom({ [APP_FRAME_TAG]: 1, type: 'boot' }, el.contentWindow)
  const load = (el: HTMLIFrameElement) => el.dispatchEvent(new Event('load'))

  it('a frame whose document boots stays on its URL and gets init with its relay', async () => {
    vi.useFakeTimers()
    const el = document.createElement('iframe')
    loadMintedFrame(el, document.body, { ...MINTED, document: DOC }, { theme: () => 'light', onNavigatedAway: () => {} })
    expect(el.getAttribute('src')).toBe(MINTED.frame)
    boot(el)
    await vi.advanceTimersByTimeAsync(BOOT_GRACE_MS * 2)
    expect(el.getAttribute('src')).toBe(MINTED.frame)
    expect(el.srcdoc).toBe('')
    const init = sentTo(el).find((m) => m.type === 'init')
    expect(init).toMatchObject({ capability: 'cap', theme: 'light', base: '' })
    expect(String(init?.relay)).toMatch(/^[0-9a-f]{32}$/)
    vi.useRealTimers()
  })

  it('a URL load that never boots (a proxy login page) falls back to srcdoc', async () => {
    vi.useFakeTimers()
    const el = document.createElement('iframe')
    const away = vi.fn()
    loadMintedFrame(el, document.body, { ...MINTED, document: DOC }, { theme: () => 'light', onNavigatedAway: away })
    // The test DOM fires the URL load itself.
    await vi.advanceTimersByTimeAsync(0)
    expect(sentTo(el).find((m) => m.type === 'init')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(BOOT_GRACE_MS)
    expect(el.srcdoc).toBe(`<meta http-equiv="Content-Security-Policy" content="script-src ${location.origin} 'nonce-N'; connect-src ${location.origin}">`)
    await vi.advanceTimersByTimeAsync(0)
    if (!sentTo(el).some((m) => m.type === 'init')) load(el)
    expect(away).not.toHaveBeenCalled()
    const init = sentTo(el).find((m) => m.type === 'init')
    expect(init).toMatchObject({ capability: 'cap', base: '' })
    vi.useRealTimers()
    // The relay serves this frame under that nonce, for its own app only.
    const fetch = vi.fn(async () => new Response('export {}', { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    relayFrom(el, { nonce: init?.relay, path: '/apps/cards/ui/dist/lp.mjs' })
    await flush(); await flush()
    expect(fetch).toHaveBeenCalledTimes(1)
    relayFrom(el, { id: 2, nonce: init?.relay, path: '/apps/other/ui/dist/lp.mjs' })
    await flush()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('the boot listener is gone once the frame booted and got init', async () => {
    const added: EventListenerOrEventListenerObject[] = []
    const removed: EventListenerOrEventListenerObject[] = []
    const add = vi.spyOn(window, 'addEventListener').mockImplementation(function (this: Window, t: string, f: EventListenerOrEventListenerObject) { if (t === 'message') added.push(f) })
    const rem = vi.spyOn(window, 'removeEventListener').mockImplementation(function (this: Window, t: string, f: EventListenerOrEventListenerObject) { if (t === 'message') removed.push(f) })
    const el = document.createElement('iframe')
    // A detached root: the test DOM fires no load of its own, so the frame can say
    // boot while it parses, before its load, as a browser delivers it.
    const root = document.createElement('div')
    loadMintedFrame(el, root, { ...MINTED, document: DOC }, { theme: () => 'light', onNavigatedAway: () => {} })
    add.mockRestore()
    // The frame relay's listener stays (it serves the frame); every other one goes.
    const own = added.slice(0, -1)
    expect(own.length).toBeGreaterThan(0)
    const [listener] = own
    const ev = new MessageEvent('message', { data: { [APP_FRAME_TAG]: 1, type: 'boot' } })
    Object.defineProperty(ev, 'source', { value: el.contentWindow })
    for (const f of own) (typeof f === 'function' ? f : f.handleEvent).call(window, ev)
    el.dispatchEvent(new Event('load'))
    await new Promise((r) => setTimeout(r, 0))
    expect(sentTo(el).filter((m) => m.type === 'init')).toHaveLength(1)
    rem.mockRestore()
    expect(own.every((f) => removed.includes(f))).toBe(true)
    void listener
  })

  it('a frame removed before its first load leaves no boot listener behind', async () => {
    vi.useFakeTimers()
    const added: EventListenerOrEventListenerObject[] = []
    const removed: EventListenerOrEventListenerObject[] = []
    const add = vi.spyOn(window, 'addEventListener').mockImplementation(function (this: Window, t: string, f: EventListenerOrEventListenerObject) { if (t === 'message') added.push(f) })
    const rem = vi.spyOn(window, 'removeEventListener').mockImplementation(function (this: Window, t: string, f: EventListenerOrEventListenerObject) { if (t === 'message') removed.push(f) })
    const root = document.createElement('div')
    document.body.append(root)
    const el = document.createElement('iframe')
    loadMintedFrame(el, root, { ...MINTED, document: DOC }, { theme: () => 'light', onNavigatedAway: () => {} })
    // Cancelled before it loaded: the host removes the frame.
    el.remove()
    await vi.advanceTimersByTimeAsync(RELAY_SWEEP_MS + BOOT_GRACE_MS)
    add.mockRestore()
    rem.mockRestore()
    vi.useRealTimers()
    expect(added.length).toBeGreaterThan(0)
    expect(added.every((f) => removed.includes(f))).toBe(true)
  })

  it('without a document, the frame loads its URL and never falls back', async () => {
    vi.useFakeTimers()
    const el = document.createElement('iframe')
    loadMintedFrame(el, document.body, MINTED, { theme: () => 'light', onNavigatedAway: () => {} })
    await vi.advanceTimersByTimeAsync(BOOT_GRACE_MS * 2)
    expect(el.getAttribute('src')).toBe(MINTED.frame)
    expect(sentTo(el).find((m) => m.type === 'init')).toMatchObject({ type: 'init', capability: 'cap', theme: 'light' })
    vi.useRealTimers()
  })
})
