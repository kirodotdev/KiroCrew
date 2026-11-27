import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { READ_OUTBOX_KEY, READ_OUTBOX_MAX, ackRead, queueRead, queuedReads, resendKeptReads } from '../lib/readOutbox'

const EARLY = '2026-11-13T10:00:00+00:00'
const LATE = '2026-11-13T11:00:00+00:00'

describe('readOutbox', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); Reflect.deleteProperty(navigator, 'locks') })

  it('keeps the newest read per slot, by instant', () => {
    queueRead('a', LATE)
    queueRead('a', EARLY)
    queueRead('b', '2026-11-13T16:00:00+05:00')
    queueRead('b', '2026-11-13T12:00:00+00:00')
    expect(queuedReads()).toEqual({ a: LATE, b: '2026-11-13T12:00:00+00:00' })
  })

  it('ignores a read with no usable time', () => {
    queueRead('a', undefined)
    queueRead('a', 'not a time')
    expect(queuedReads()).toEqual({})
  })

  it('survives a reload: the kept reads live in localStorage', () => {
    queueRead('a', LATE)
    expect(JSON.parse(localStorage.getItem(READ_OUTBOX_KEY) ?? '{}')).toEqual({ a: LATE })
  })

  it('forgets a read only when the stored read covers it', () => {
    queueRead('a', LATE)
    ackRead('a', EARLY)
    ackRead('a', undefined)
    expect(queuedReads()).toEqual({ a: LATE })
    ackRead('a', LATE)
    expect(queuedReads()).toEqual({})
  })

  it('loads and stores under one lock every window shares, so a write in between cannot drop a read', () => {
    const names: string[] = []
    const held: Array<() => void> = []
    Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: (name: string, cb: () => void) => { names.push(name); held.push(cb); return Promise.resolve() } } })
    localStorage.setItem(READ_OUTBOX_KEY, JSON.stringify({ a: EARLY }))
    const realGet = Storage.prototype.getItem
    // Another window queues a read between this window's load and its store.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementationOnce(function (this: Storage, key: string) { const stale = realGet.call(this, key); queueRead('b', LATE); return stale })
    ackRead('a', EARLY)
    while (held.length) held.shift()?.()
    expect(names).toEqual([READ_OUTBOX_KEY, READ_OUTBOX_KEY])
    expect(queuedReads()).toEqual({ b: LATE })
  })

  it('drops the oldest reads past the cap', () => {
    const base = Date.parse(EARLY)
    for (let i = 0; i <= READ_OUTBOX_MAX; i++) queueRead(`s${i}`, new Date(base + i * 1000).toISOString())
    const kept = queuedReads()
    expect(Object.keys(kept)).toHaveLength(READ_OUTBOX_MAX)
    expect(kept.s0).toBeUndefined()
    expect(kept[`s${READ_OUTBOX_MAX}`]).toBeDefined()
  })

  it('resends each kept read once and keeps it until confirmed', () => {
    queueRead('a', EARLY)
    queueRead('b', LATE)
    const sent: Array<[string, string]> = []
    resendKeptReads((slot, ts) => { sent.push([slot, ts]) })
    expect(sent).toEqual([['a', EARLY], ['b', LATE]])
    expect(queuedReads()).toEqual({ a: EARLY, b: LATE })
  })

  it('resends a read still waiting for the lock, and counts it as queued meanwhile', () => {
    const held: Array<() => void> = []
    Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: (_: string, cb: () => void) => { held.push(cb); return Promise.resolve() } } })
    queueRead('a', LATE)
    expect(queuedReads()).toEqual({ a: LATE })
    const send = vi.fn()
    resendKeptReads(send)
    while (held.length) held.shift()?.()
    expect(send.mock.calls).toEqual([['a', LATE]])
  })

  it('replays a read whose storage write failed while disconnected, until the stored echo covers it', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('full', 'QuotaExceededError') })
    queueRead('q', LATE)
    const send = vi.fn()
    resendKeptReads(send)
    ackRead('q', LATE)
    expect([send.mock.calls, queuedReads()]).toEqual([[['q', LATE]], {}])
  })

  it('reads a corrupt record as empty', () => {
    localStorage.setItem(READ_OUTBOX_KEY, '{oops')
    expect(queuedReads()).toEqual({})
    localStorage.setItem(READ_OUTBOX_KEY, JSON.stringify(['a']))
    expect(queuedReads()).toEqual({})
  })
})
