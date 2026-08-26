import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  drainWindow,
  recordCacheKey,
  startPierrePerfReporting,
  stopPierrePerfReporting,
} from '../lib/pierrePerf'

interface TestWindow {
  electronAPI?: { reportPierrePerf?: (w: unknown) => void }
}

function setApi(fn: ((w: unknown) => void) | undefined) {
  const w = window as unknown as TestWindow
  if (fn === undefined) {
    delete w.electronAPI
    return
  }
  w.electronAPI = { reportPierrePerf: fn }
}

afterEach(() => {
  stopPierrePerfReporting()
  setApi(undefined)
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('pierrePerf accounting', () => {
  // Accounting is gated on a reporter existing, so these tests go through the real
  // gate (stub an API, then start) rather than around it. Fake timers keep the
  // interval from firing inside the accounting cases.
  beforeEach(() => {
    vi.useFakeTimers()
    setApi(vi.fn())
    startPierrePerfReporting()
  })

  it('records nothing at all when no reporter exists', () => {
    // The leak guard: in a plain browser there is no electronAPI, so no interval
    // ever drains the counters. If accounting still ran, keysBySurface would retain
    // every key for the life of the session -- the very failure this module was
    // built to diagnose. Nothing is accumulated, so there is nothing to leak.
    stopPierrePerfReporting()
    setApi(undefined)
    startPierrePerfReporting()
    for (let i = 0; i < 50; i++) recordCacheKey('file', 'f.ts', `f.ts:${i}:h`, i)
    expect(drainWindow()).toBeNull()
  })

  it('reports nothing when no highlighting happened', () => {
    expect(drainWindow()).toBeNull()
  })

  it('sums calls, chars and the largest content length', () => {
    recordCacheKey('file', 'a.ts', 'a.ts:3:x', 3)
    recordCacheKey('file', 'b.ts', 'b.ts:10:y', 10)
    const w = drainWindow()
    expect(w).not.toBeNull()
    expect(w?.calls).toBe(2)
    expect(w?.chars).toBe(13)
    expect(w?.maxLen).toBe(10)
  })

  it('counts distinct keys, so repeated identical renders are not miscounted as churn', () => {
    // Same key three times = Pierre serves cached tokens; only one tokenize.
    recordCacheKey('file', 'a.ts', 'a.ts:5:same', 5)
    recordCacheKey('file', 'a.ts', 'a.ts:5:same', 5)
    recordCacheKey('file', 'a.ts', 'a.ts:5:same', 5)
    const w = drainWindow()
    expect(w?.calls).toBe(3)
    expect(w?.keys).toBe(1)
    // The two re-hashes of unchanged content are a memoization miss, reported
    // separately so they cannot be read as key churn.
    expect(w?.repeatKeyCalls).toBe(2)
    expect(w?.maxKeysForOneSurface).toBe(1)
  })

  it('exposes the quadratic signature of a streamed block', () => {
    // A block streamed in 4 growing chunks: every chunk mints a new key, and the
    // hashed characters are the sum of the prefixes, not the final length.
    for (const len of [10, 20, 30, 40]) recordCacheKey('file', 'f.ts', `f.ts:${len}:h`, len)
    const w = drainWindow()
    expect(w?.keys).toBe(4)
    expect(w?.maxLen).toBe(40)
    expect(w?.chars).toBe(100)
    // The verdict: one NAME minted 4 keys, which is what churn looks like.
    expect(w?.maxKeysForOneSurface).toBe(4)
    expect(w?.repeatKeyCalls).toBe(0)
  })

  it('does NOT report churn when many distinct blocks are each rendered once', () => {
    // The ambiguity this field exists to remove: four separate files of the same
    // sizes as the streamed case above produce IDENTICAL calls/keys/chars/maxLen,
    // so the aggregate ratio cannot tell the two apart. maxKeysForOneSurface can.
    for (const len of [10, 20, 30, 40]) recordCacheKey('file', `f${len}.ts`, `f${len}.ts:${len}:h`, len)
    const w = drainWindow()
    expect(w?.calls).toBe(4)
    expect(w?.keys).toBe(4)
    expect(w?.chars).toBe(100)
    expect(w?.maxLen).toBe(40)
    // Same ratio as the streamed block (2.5) -- and yet no churn.
    expect((w as { chars: number }).chars / (w as { maxLen: number }).maxLen).toBe(2.5)
    expect(w?.maxKeysForOneSurface).toBe(1)
  })

  it('does NOT report churn when two independent fences share one name', () => {
    // The collision the instance qualifier exists to remove: an agent showing
    // before/after as two SEPARATE fences of the same file name. Under a
    // kind+name identity both landed in one bucket and read as churn; with the
    // caller's useId prefix they are two instances, one key each.
    recordCacheKey(':r1:', 'snippet.ts', 'snippet.ts:100:before', 100)
    recordCacheKey(':r2:', 'snippet.ts', 'snippet.ts:120:after', 120)
    const w = drainWindow()
    expect(w?.calls).toBe(2)
    expect(w?.keys).toBe(2)
    expect(w?.maxKeysForOneSurface).toBe(1)
  })

  it('does NOT report churn when a settled diff keys both sides of one filename', () => {
    // The false confirm that per-NAME attribution produced: a two-sided diff of
    // an edit keys oldFile and newFile under the SAME filename (PierreImpl
    // renders them as the 'diff-old' and 'diff-new' surfaces), so a per-name
    // count read every static diff as 2. Per surface, each side is its own
    // once-rendered unit and the verdict stays at the floor.
    recordCacheKey('diff-old', 'a.ts', 'a.ts:100:old', 100)
    recordCacheKey('diff-new', 'a.ts', 'a.ts:120:new', 120)
    const w = drainWindow()
    expect(w?.calls).toBe(2)
    expect(w?.keys).toBe(2)
    expect(w?.maxKeysForOneSurface).toBe(1)
    expect(w?.repeatKeyCalls).toBe(0)
  })

  it('attributes churn to the busiest surface when blocks are interleaved', () => {
    // A streaming block alongside quiet ones: the verdict must follow the block
    // that actually churned, not the count of names.
    recordCacheKey('file', 'quiet.ts', 'quiet.ts:5:a', 5)
    recordCacheKey('file', 'other.ts', 'other.ts:5:b', 5)
    for (const len of [10, 20, 30]) recordCacheKey('file', 'busy.ts', `busy.ts:${len}:h`, len)
    const w = drainWindow()
    expect(w?.keys).toBe(5)
    expect(w?.maxKeysForOneSurface).toBe(3)
  })

  it('drains, so each window is independent', () => {
    recordCacheKey('file', 'a.ts', 'a.ts:1:x', 1)
    drainWindow()
    expect(drainWindow()).toBeNull()
  })

  it('always reports a numeric heap field', () => {
    recordCacheKey('file', 'a.ts', 'a.ts:1:x', 1)
    const w = drainWindow()
    expect(typeof w?.heapMB).toBe('number')
  })

  it('reports -1 rather than throwing when the heap probe is unavailable', () => {
    // performance.memory is a non-standard Chromium extension. It is absent in
    // this environment, so it is installed as a THROWING getter to prove the
    // read is guarded: a host where probing throws must degrade to -1, not take
    // the render path down with it.
    const had = Object.prototype.hasOwnProperty.call(performance, 'memory')
    Object.defineProperty(performance, 'memory', {
      configurable: true,
      get() {
        throw new Error('nope')
      },
    })
    try {
      recordCacheKey('file', 'a.ts', 'a.ts:1:x', 1)
      expect(drainWindow()?.heapMB).toBe(-1)
    } finally {
      if (!had) delete (performance as unknown as { memory?: unknown }).memory
    }
  })

  it('reports the heap in MB when the probe is available', () => {
    const had = Object.prototype.hasOwnProperty.call(performance, 'memory')
    Object.defineProperty(performance, 'memory', {
      configurable: true,
      get() {
        return { usedJSHeapSize: 3 * 1024 * 1024 }
      },
    })
    try {
      recordCacheKey('file', 'a.ts', 'a.ts:1:x', 1)
      expect(drainWindow()?.heapMB).toBe(3)
    } finally {
      if (!had) delete (performance as unknown as { memory?: unknown }).memory
    }
  })
})

describe('pierrePerf reporting lifecycle', () => {
  it('does nothing outside Electron, where there is no main process to log to', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(undefined)
    startPierrePerfReporting()
    recordCacheKey('file', 'a.ts', 'a.ts:1:x', 1)
    vi.advanceTimersByTime(60_000)
    expect(spy).not.toHaveBeenCalled()
    // Nothing was recorded either: with no reporter to drain the counters,
    // accumulating them would be an unbounded retain for the whole session.
    expect(drainWindow()).toBeNull()
  })

  it('reports a non-empty window on the interval', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(spy)
    startPierrePerfReporting()
    recordCacheKey('file', 'f.ts', 'f.ts:10:h', 10)
    recordCacheKey('file', 'f.ts', 'f.ts:20:h', 20)
    vi.advanceTimersByTime(5000)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0][0]).toMatchObject({ calls: 2, keys: 2, chars: 30, maxLen: 20 })
  })

  it('sends nothing while idle, so a quiet session reports nothing at all', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(spy)
    startPierrePerfReporting()
    vi.advanceTimersByTime(30_000)
    expect(spy).not.toHaveBeenCalled()
  })

  it('drains between intervals, so a window is never reported twice', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(spy)
    startPierrePerfReporting()
    recordCacheKey('file', 'f.ts', 'f.ts:10:h', 10)
    vi.advanceTimersByTime(5000)
    vi.advanceTimersByTime(5000)
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('survives a throwing reporter — diagnostics never break rendering', () => {
    vi.useFakeTimers()
    const spy = vi.fn(() => {
      throw new Error('ipc gone')
    })
    setApi(spy)
    startPierrePerfReporting()
    recordCacheKey('file', 'f.ts', 'f.ts:10:h', 10)
    expect(() => vi.advanceTimersByTime(5000)).not.toThrow()
    expect(spy).toHaveBeenCalledTimes(1)
    // Still reporting after the failure.
    recordCacheKey('file', 'f.ts', 'f.ts:11:h', 11)
    expect(() => vi.advanceTimersByTime(5000)).not.toThrow()
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('is idempotent, so a second call cannot double-report', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(spy)
    startPierrePerfReporting()
    startPierrePerfReporting()
    recordCacheKey('file', 'f.ts', 'f.ts:10:h', 10)
    vi.advanceTimersByTime(5000)
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('stops reporting once stopped', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(spy)
    startPierrePerfReporting()
    stopPierrePerfReporting()
    recordCacheKey('file', 'f.ts', 'f.ts:10:h', 10)
    vi.advanceTimersByTime(30_000)
    expect(spy).not.toHaveBeenCalled()
  })

  it('can be restarted after a stop', () => {
    vi.useFakeTimers()
    const spy = vi.fn()
    setApi(spy)
    startPierrePerfReporting()
    stopPierrePerfReporting()
    startPierrePerfReporting()
    recordCacheKey('file', 'f.ts', 'f.ts:10:h', 10)
    vi.advanceTimersByTime(5000)
    expect(spy).toHaveBeenCalledTimes(1)
  })
})
