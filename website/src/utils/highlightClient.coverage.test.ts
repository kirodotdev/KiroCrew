import { describe, it, expect, vi, beforeEach, afterEach, onTestFinished } from 'vitest'

// The sanitizer is a collaborator: happy-dom does not reproduce DOMPurify's
// output, so it is stubbed here with a stripper that removes script elements.
vi.mock('dompurify', () => ({
  default: {
    sanitize: (html: string) => html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ''),
  },
}))

// The main-thread client keeps its worker and pending requests in module state,
// so every test loads a fresh copy of the module against a fake Worker.
type Reply = { id: number; html: string }

class FakeWorker {
  static instances: FakeWorker[] = []
  onmessage: ((e: { data: Reply }) => void) | null = null
  onerror: (() => void) | null = null
  posted: Array<{ id: number; code: string; lang?: string }> = []
  terminated = false
  constructor(public url: string) {
    FakeWorker.instances.push(this)
  }
  postMessage(msg: { id: number; code: string; lang?: string }) {
    this.posted.push(msg)
  }
  terminate() {
    this.terminated = true
  }
}

type Client = typeof import('./highlightClient')
let client: Client

beforeEach(async () => {
  FakeWorker.instances = []
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  client = await import('./highlightClient')
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const reply = (w: FakeWorker, r: Reply) => w.onmessage?.({ data: r })

describe('highlightAsync', () => {
  it('resolves to empty text when the environment has no Worker', async () => {
    vi.stubGlobal('Worker', undefined)
    await expect(client.highlightAsync('const a = 1', 'js')).resolves.toBe('')
    expect(FakeWorker.instances).toHaveLength(0)
  })

  it('posts the code and language to the worker and resolves to sanitized html', async () => {
    const p = client.highlightAsync('const a = 1', 'typescript')
    const w = FakeWorker.instances[0]
    expect(w.url).toContain('hljsWorker')
    expect(w.posted).toEqual([{ id: 1, code: 'const a = 1', lang: 'typescript' }])
    reply(w, { id: 1, html: '<b>hi</b><script>alert(1)</script>' })
    await expect(p).resolves.toBe('<b>hi</b>')
  })

  it('shares one worker and routes each reply to its own request', async () => {
    const first = client.highlightAsync('a', 'js')
    const second = client.highlightAsync('b', 'py')
    expect(FakeWorker.instances).toHaveLength(1)
    const w = FakeWorker.instances[0]
    expect(w.posted.map((m) => m.id)).toEqual([1, 2])

    reply(w, { id: 2, html: '<i>second</i>' })
    await expect(second).resolves.toBe('<i>second</i>')
    reply(w, { id: 1, html: '<i>first</i>' })
    await expect(first).resolves.toBe('<i>first</i>')
  })

  it('ignores a reply whose id matches no pending request', async () => {
    const p = client.highlightAsync('a', 'js')
    const w = FakeWorker.instances[0]
    reply(w, { id: 99, html: '<b>stray</b>' })
    reply(w, { id: 1, html: '<b>own</b>' })
    await expect(p).resolves.toBe('<b>own</b>')
  })

  it('falls back to plain text for every pending request when the worker errors', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const p = client.highlightAsync('a', 'js')
    const w = FakeWorker.instances[0]
    w.onerror?.()
    await expect(p).resolves.toBe('')
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('falling back to plain text'))
    expect(w.terminated).toBe(false)

    // The worker is given up for the rest of the session: no new one is built.
    await expect(client.highlightAsync('b', 'js')).resolves.toBe('')
    expect(FakeWorker.instances).toHaveLength(1)
  })

  it('falls back to plain text when the worker cannot be constructed', async () => {
    class ThrowingWorker {
      constructor() {
        throw new Error('blocked by CSP')
      }
    }
    vi.stubGlobal('Worker', ThrowingWorker)
    await expect(client.highlightAsync('a', 'js')).resolves.toBe('')
  })

  it('resolves to plain text and recycles a wedged worker after the timeout', async () => {
    vi.useFakeTimers()
    onTestFinished(() => vi.useRealTimers())
    const p = client.highlightAsync('a', 'js')
    const w = FakeWorker.instances[0]

    vi.advanceTimersByTime(3000)
    await expect(p).resolves.toBe('')
    expect(w.terminated).toBe(true)

    // The next request gets a fresh worker.
    void client.highlightAsync('b', 'js')
    expect(FakeWorker.instances).toHaveLength(2)
  })

  it('does not terminate a replacement worker when an older request times out', async () => {
    vi.useFakeTimers()
    onTestFinished(() => vi.useRealTimers())
    void client.highlightAsync('a', 'js')
    const w1 = FakeWorker.instances[0]
    vi.advanceTimersByTime(1000)
    void client.highlightAsync('b', 'js')

    // The first request times out and recycles w1, then a third request starts w2.
    vi.advanceTimersByTime(2000)
    expect(w1.terminated).toBe(true)
    const third = client.highlightAsync('c', 'js')
    const w2 = FakeWorker.instances[1]

    // The second request's own timer now fires against the replacement worker.
    vi.advanceTimersByTime(1000)
    expect(w2.terminated).toBe(false)

    reply(w2, { id: 3, html: '<u>third</u>' })
    await expect(third).resolves.toBe('<u>third</u>')
  })
})
