/**
 * Reads take turns, so a send always finds a free connection.
 *
 * A browser opens six connections to the gateway. Slow re-reads (worker lists,
 * the board, the roster) used to hold all six, and a send then waited in the
 * browser's queue until the composer's timer gave up on it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from './client'
import { READ_GATE_WIDTH, __resetReadGateForTests, gatedRead } from './readGate'
import { readCrewWorkers, resetCrewWorkersPoll } from '../lib/crewWorkersPoll'

interface Call { url: string; method: string; answer: (r: Response) => void; fail: (e: unknown) => void }
let calls: Call[] = []

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
/** Let the promise chains between an answer and the next read's start run. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0))
const reads = () => calls.filter(c => c.method === 'GET').length

beforeEach(() => {
  calls = []
  __resetReadGateForTests()
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => new Promise<Response>((answer, fail) => {
    calls.push({ url: String(url), method: (init?.method ?? 'GET').toUpperCase(), answer, fail })
  })))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('reads to the gateway', () => {
  it('go out at most four at a time, and a send goes out past them at once', async () => {
    const lists = Array.from({ length: 6 }, (_, i) => api.crewWorkers(`root-${i}`).catch(() => undefined))
    expect(reads()).toBe(READ_GATE_WIDTH)

    void api.sendChat('hello', 'slot-1')
    expect(calls.at(-1)).toMatchObject({ method: 'POST', url: '/api/chat?ws=1' })
    expect(reads()).toBe(READ_GATE_WIDTH)

    // One answer arrives whole: the next read in line takes its place.
    calls[0].answer(json({ workers: [] }))
    await settle()
    expect(reads()).toBe(READ_GATE_WIDTH + 1)
    expect(calls.at(-1)?.url).toContain('root-4')

    for (const call of calls) call.answer(json({ workers: [] }))
    await settle()
    for (const call of calls) call.answer(json({ workers: [] }))
    await Promise.all(lists)
    expect(reads()).toBe(6)
  })

  it('a failed read gives its place to the next one', async () => {
    for (let i = 0; i < 5; i++) void api.crewWorkers(`root-${i}`).catch(() => undefined)
    expect(reads()).toBe(4)
    calls[1].fail(new TypeError('Failed to fetch'))
    await settle()
    expect(reads()).toBe(5)
  })

  it('an opened transcript goes out ahead of re-reads asked for before it', async () => {
    // Four re-reads are out and two more wait, as on a cold open.
    for (let i = 0; i < 6; i++) void api.crewWorkers(`root-${i}`).catch(() => undefined)
    void api.chatSlotDetail('chat-1', 100).catch(() => undefined)
    await settle()
    // It does not take a fifth place: a send must still find a free connection.
    expect(reads()).toBe(READ_GATE_WIDTH)

    // Each answer frees one place, and the next read out is the next one below.
    const next = async (at: number) => {
      calls[at].answer(json({ workers: [] }))
      await settle()
      return calls.at(-1)?.url
    }
    expect(await next(0)).toContain('/api/chat/slots/chat-1?')
    // The re-reads that were already waiting follow, in the order they were asked for.
    expect(await next(1)).toContain('root-4')
    expect(await next(2)).toContain('root-5')
  })

  it('worker lists keep their own cap of two inside the four, and nothing is left waiting', async () => {
    resetCrewWorkersPoll()
    const lists = Array.from({ length: 5 }, (_, i) => readCrewWorkers(`root-${i}`))
    await settle()
    const boards = Array.from({ length: 4 }, (_, i) => api.crewBoard(`c-${i}`).catch(() => undefined))
    await settle()
    const out = (part: string) => calls.filter(c => c.url.includes(part)).length
    expect(out('/crew-workers')).toBe(2)
    expect(out('/api/crew-board')).toBe(2)

    for (let round = 0; round < 10; round++) {
      for (const call of calls) call.answer(json(call.url.includes('/crew-workers') ? { workers: [] } : { items: [] }))
      await settle()
    }
    await Promise.all([...lists, ...boards])
    expect(out('/crew-workers')).toBe(5)
    expect(out('/api/crew-board')).toBe(4)
  })
})

describe('gatedRead', () => {
  const never = () => new Promise<Response>(() => {})

  it('holds its place until the body has arrived, not only the headers', async () => {
    let finish!: () => void
    const body = new ReadableStream<Uint8Array>({ start(controller) { finish = () => controller.close() } })
    for (let i = 0; i < READ_GATE_WIDTH - 1; i++) void gatedRead('/api/x', null, never)
    void gatedRead('/api/x', null, () => Promise.resolve(new Response(body)))
    const started = vi.fn(never)
    void gatedRead('/api/x', null, started)
    await settle()
    expect(started).not.toHaveBeenCalled()
    finish()
    await settle()
    expect(started).toHaveBeenCalledTimes(1)
  })

  it('a read aborted while it waits is never sent', async () => {
    for (let i = 0; i < READ_GATE_WIDTH; i++) void gatedRead('/api/x', null, never)
    const controller = new AbortController()
    const started = vi.fn(never)
    const waiting = gatedRead('/api/x', controller.signal, started)
    controller.abort()
    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' })
    expect(started).not.toHaveBeenCalled()
  })

  it('reads asked for first go out before the others, each kind in its own order', async () => {
    const finish: Array<() => void> = []
    for (let i = 0; i < READ_GATE_WIDTH; i++) {
      void gatedRead('/api/x', null, () => new Promise<Response>(resolve => { finish.push(() => resolve(new Response(null))) }))
    }
    const order: string[] = []
    const read = (name: string, first: boolean) => void gatedRead('/api/x', null, () => { order.push(name); return never() }, first)
    read('plain-1', false)
    read('first-1', true)
    read('plain-2', false)
    read('first-2', true)
    expect(order).toEqual([])
    for (const done of finish) {
      done()
      await settle()
    }
    expect(order).toEqual(['first-1', 'first-2', 'plain-1', 'plain-2'])
  })

  it('a read asked for first and aborted while it waits is never sent, and the next one is', async () => {
    let finish!: () => void
    for (let i = 0; i < READ_GATE_WIDTH - 1; i++) void gatedRead('/api/x', null, never)
    void gatedRead('/api/x', null, () => new Promise<Response>(resolve => { finish = () => resolve(new Response(null)) }))
    const controller = new AbortController()
    const left = vi.fn(never)
    const waiting = gatedRead('/api/x', controller.signal, left, true)
    const next = vi.fn(never)
    void gatedRead('/api/x', null, next)
    controller.abort()
    await expect(waiting).rejects.toMatchObject({ name: 'AbortError' })
    finish()
    await settle()
    expect(left).not.toHaveBeenCalled()
    expect(next).toHaveBeenCalledTimes(1)
  })

  it('counts each origin on its own', () => {
    for (let i = 0; i < READ_GATE_WIDTH; i++) void gatedRead('/api/x', null, never)
    const other = vi.fn(never)
    void gatedRead('http://127.0.0.1:5999/api/x', null, other)
    expect(other).toHaveBeenCalledTimes(1)
  })

  it('a read whose start returns no promise rejects and gives its place back', async () => {
    let finish!: (r: Response) => void
    for (let i = 0; i < READ_GATE_WIDTH - 1; i++) void gatedRead('/api/x', null, never)
    void gatedRead('/api/x', null, () => new Promise<Response>(resolve => { finish = resolve }))
    const broken = gatedRead('/api/x', null, () => undefined as unknown as Promise<Response>)
    const started = vi.fn(never)
    void gatedRead('/api/x', null, started)
    finish(new Response(null))
    await expect(broken).rejects.toBeInstanceOf(TypeError)
    await settle()
    expect(started).toHaveBeenCalledTimes(1)
  })
})
