import { describe, it, expect, afterEach } from 'vitest'
import { runQueuedUpload, UPLOAD_CONCURRENCY, __resetUploadQueueForTests } from '../utils/uploadQueue'

afterEach(() => { __resetUploadQueueForTests() })

/** Let settled requests run their hand-off to the next waiting one. */
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }

/** A request that settles when the case says, counting how many run at once. */
function harness() {
  let inFlight = 0
  let peak = 0
  const settle: Array<() => void> = []
  const start = (name: string, log: string[]) => () => {
    log.push(name)
    inFlight++
    peak = Math.max(peak, inFlight)
    return new Promise<string>(resolve => { settle.push(() => { inFlight--; resolve(name) }) })
  }
  return { start, settle, peak: () => peak }
}

describe('runQueuedUpload', () => {
  it('runs at most UPLOAD_CONCURRENCY at once, in start order, and every one resolves', async () => {
    expect(UPLOAD_CONCURRENCY).toBe(3)
    const h = harness()
    const log: string[] = []
    const results = ['a', 'b', 'c', 'd', 'e'].map(n => runQueuedUpload(new AbortController().signal, h.start(n, log)))
    expect(log).toEqual(['a', 'b', 'c'])
    h.settle.shift()!()
    await flush()
    expect(log).toEqual(['a', 'b', 'c', 'd'])
    while (h.settle.length) { h.settle.shift()!(); await flush() }
    await expect(Promise.all(results)).resolves.toEqual(['a', 'b', 'c', 'd', 'e'])
    expect(h.peak()).toBe(3)
  })

  it('drops a waiting request whose signal aborts: it never starts and rejects with AbortError', async () => {
    const h = harness()
    const log: string[] = []
    const controllers = [0, 1, 2, 3, 4].map(() => new AbortController())
    const results = controllers.map((c, i) => runQueuedUpload(c.signal, h.start(`f${i}`, log)))
    controllers[3].abort()
    await expect(results[3]).rejects.toMatchObject({ name: 'AbortError' })
    while (h.settle.length) { h.settle.shift()!(); await flush() }
    await Promise.all([results[0], results[1], results[2], results[4]])
    expect(log).toEqual(['f0', 'f1', 'f2', 'f4'])
  })

  it('rejects at once for a signal already aborted, and frees the slot when a request throws', async () => {
    const aborted = new AbortController()
    aborted.abort()
    const log: string[] = []
    await expect(runQueuedUpload(aborted.signal, async () => { log.push('never') })).rejects.toMatchObject({ name: 'AbortError' })
    expect(log).toEqual([])
    const failures = [0, 1, 2].map(() => runQueuedUpload(new AbortController().signal, () => { throw new Error('boom') }))
    for (const f of failures) await expect(f).rejects.toThrow('boom')
    await expect(runQueuedUpload(new AbortController().signal, async () => 'next')).resolves.toBe('next')
  })
})
