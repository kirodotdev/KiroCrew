import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ROOM_KEY, ROOM_VERSION } from './room'
import type { SinceBot } from './since'
import { useSince, type SinceKept } from './useSince'

const bot = (slug: string, over: Partial<SinceBot> = {}): SinceBot => ({ slug, state: 'idle', lastTs: 100, ownUnread: false, needs: [], cuts: [], ...over })
const visit = { seen: { at: 1_000_000, bots: { juno: { asks: [], cuts: [], ts: 100 }, pico: { asks: [], cuts: [], ts: 100 } } } }

afterEach(() => window.localStorage.removeItem(ROOM_KEY))

describe('the since line waits for a whole read (REVIEW-15 P4 M1)', () => {
  it('a read that is not whole neither enters, nor drops, nor changes what the record gets', () => {
    window.localStorage.setItem(ROOM_KEY, JSON.stringify({ v: ROOM_VERSION, ...visit }))
    const kept = { current: null as SinceKept | null }
    const whole = [bot('juno', { state: 'needs', needs: [{ id: 'a1' }] }), bot('pico', { state: 'failed', cuts: [{ id: 'c1', stale: false }] })]
    // A part-list: the sessions are not loaded, so no Bot asks and none is cut.
    const part = [bot('juno'), bot('pico')]
    const { result, rerender } = renderHook(({ bots, known }) => useSince(bots, known, () => 2_000_000, kept, () => {}, new Set()), { initialProps: { bots: part, known: false } })
    expect(result.current.line).toBeNull()
    expect(kept.current?.seen()).toEqual(visit.seen)
    rerender({ bots: whole, known: true })
    expect(result.current.line?.counts).toEqual({ needs: 1, failed: 1, finished: 0 })
    // The list goes part again (a gateway that restores): nothing drops, and the record keeps the whole read.
    rerender({ bots: part, known: false })
    expect(result.current.line?.counts).toEqual({ needs: 1, failed: 1, finished: 0 })
    expect(kept.current?.seen()?.bots.juno.asks).toEqual(['a1'])
    expect(kept.current?.seen()?.bots.pico.cuts).toEqual(['c1'])
  })
})

describe('a sheet in his hand (D39, ruling 1)', () => {
  it('puts out its Bot\'s light and takes it from the line, whatever its group', () => {
    window.localStorage.setItem(ROOM_KEY, JSON.stringify({ v: ROOM_VERSION, ...visit }))
    const kept = { current: null as SinceKept | null }
    const crew = [bot('juno', { lastTs: 200, ownUnread: true }), bot('pico', { state: 'failed', cuts: [{ id: 'c1', stale: false }] })]
    const { result } = renderHook(() => useSince(crew, true, () => 2_000_000, kept, () => {}, new Set()))
    expect([...result.current.lit].sort()).toEqual(['juno', 'pico'])
    act(() => { result.current.looked('juno', 'done') })
    expect([...result.current.lit]).toEqual(['pico'])
    act(() => { result.current.looked('pico', 'failed') })
    expect(result.current.line).toBeNull()
  })
})

describe('a visit ends after five minutes away (D39, ruling 3)', () => {
  const hide = (hidden: boolean) => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') })
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  }
  afterEach(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' }) })
  const run = (awayMs: number) => {
    window.localStorage.setItem(ROOM_KEY, JSON.stringify({ v: ROOM_VERSION, ...visit }))
    let clock = 2_000_000
    const kept = { current: null as SinceKept | null }
    const calm = [bot('juno'), bot('pico')]
    const { result, rerender } = renderHook(({ bots }) => useSince(bots, true, () => clock, kept, () => {}, new Set()), { initialProps: { bots: calm } })
    expect(result.current.line).toBeNull()
    hide(true)
    // While he is away Juno finishes with words; the record keeps the visit as he left it.
    clock += awayMs
    rerender({ bots: [bot('juno', { lastTs: 300, ownUnread: true }), bot('pico')] })
    expect(kept.current?.seen()?.bots.juno.ts).toBe(100)
    hide(false)
    return result
  }
  it('back after six minutes: the line is worked out again', () => {
    expect(run(6 * 60_000).current.line?.counts).toEqual({ needs: 0, failed: 0, finished: 1 })
  })
  it('back after four minutes: the same visit, no line', () => {
    expect(run(4 * 60_000).current.line).toBeNull()
  })
})
