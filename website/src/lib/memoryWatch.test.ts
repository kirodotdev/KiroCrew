import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { startMemoryWatch, stopMemoryWatch, takeSample } from './memoryWatch'

type Reporter = ReturnType<typeof vi.fn>

let reporter: Reporter

function setPerfMemory(used: number | undefined, limit = 4192 * 1024 * 1024) {
  Object.defineProperty(performance, 'memory', {
    configurable: true,
    get() {
      if (used === undefined) return undefined
      return { usedJSHeapSize: used, jsHeapSizeLimit: limit }
    },
  })
}

function setBridge(over: Record<string, unknown> = {}) {
  ;(globalThis as unknown as { electronAPI?: unknown }).electronAPI = {
    reportMemorySample: reporter,
    heapStatisticsKB: () => ({ usedHeapKB: 100 * 1024 }),
    ...over,
  }
}

beforeEach(() => {
  reporter = vi.fn()
  setPerfMemory(700 * 1024 * 1024)
  setBridge()
})

afterEach(() => {
  stopMemoryWatch()
  delete (globalThis as unknown as { electronAPI?: unknown }).electronAPI
  // Object.defineProperty leaks onto the shared global performance object, so
  // remove it rather than letting it bleed into neighbouring specs.
  delete (performance as unknown as { memory?: unknown }).memory
  vi.useRealTimers()
})

describe('takeSample', () => {
  it('derives externalKB as usedJSHeapSize minus the object heap', () => {
    // usedJSHeapSize = used_heap_size + external_memory, so the difference is
    // external. 700MB reported, 100MB object heap => 600MB external.
    const s = takeSample()
    expect(s.usedHeapKB).toBe(700 * 1024)
    expect(s.externalKB).toBe(600 * 1024)
    expect(s.limitHeapKB).toBe(4192 * 1024)
  })

  it('clamps a transiently negative subtraction to zero', () => {
    // The two readings are taken microseconds apart; a GC in between can make the
    // difference negative, which is sampling jitter rather than a real metric.
    setBridge({ heapStatisticsKB: () => ({ usedHeapKB: 900 * 1024 }) })
    expect(takeSample().externalKB).toBe(0)
  })

  it('reports externalKB as null when the object-heap half is unavailable', () => {
    // Null means "this channel does not exist in this realm" and must not be
    // faked as 0 or -1 -- the flush distinguishes unknown from frozen.
    setBridge({ heapStatisticsKB: () => null })
    const s = takeSample()
    expect(s.externalKB).toBeNull()
    expect(s.usedHeapKB).toBe(700 * 1024)
  })

  it('reports externalKB as null when heapStatisticsKB throws', () => {
    setBridge({
      heapStatisticsKB: () => {
        throw new Error('not exposed')
      },
    })
    expect(takeSample().externalKB).toBeNull()
  })

  it('degrades to all-null when performance.memory is absent', () => {
    // Non-standard Chromium extension: absent in some realms and off Chromium.
    setPerfMemory(undefined)
    const s = takeSample()
    expect(s.usedHeapKB).toBeNull()
    expect(s.limitHeapKB).toBeNull()
    expect(s.externalKB).toBeNull()
  })
})

describe('startMemoryWatch', () => {
  it('reports on the sampling interval with the realm label', () => {
    vi.useFakeTimers()
    startMemoryWatch('worker:pierre')
    expect(reporter).not.toHaveBeenCalled()
    vi.advanceTimersByTime(5000)
    expect(reporter).toHaveBeenCalledTimes(1)
    expect(reporter.mock.calls[0][0].realm).toBe('worker:pierre')
    vi.advanceTimersByTime(10000)
    expect(reporter).toHaveBeenCalledTimes(3)
  })

  it('defaults the realm to main', () => {
    vi.useFakeTimers()
    startMemoryWatch()
    vi.advanceTimersByTime(5000)
    expect(reporter.mock.calls[0][0].realm).toBe('main')
  })

  it('is idempotent so a second call does not double the cadence', () => {
    vi.useFakeTimers()
    startMemoryWatch()
    startMemoryWatch()
    vi.advanceTimersByTime(5000)
    expect(reporter).toHaveBeenCalledTimes(1)
  })

  it('no-ops without a reporter, so a plain browser pays nothing', () => {
    vi.useFakeTimers()
    delete (globalThis as unknown as { electronAPI?: unknown }).electronAPI
    expect(() => startMemoryWatch()).not.toThrow()
    vi.advanceTimersByTime(15000)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('a throwing reporter never breaks the sampling timer', () => {
    vi.useFakeTimers()
    setBridge({
      reportMemorySample: () => {
        throw new Error('ipc gone')
      },
    })
    startMemoryWatch()
    expect(() => vi.advanceTimersByTime(10000)).not.toThrow()
  })

  it('stopMemoryWatch halts reporting', () => {
    vi.useFakeTimers()
    startMemoryWatch()
    vi.advanceTimersByTime(5000)
    stopMemoryWatch()
    vi.advanceTimersByTime(20000)
    expect(reporter).toHaveBeenCalledTimes(1)
  })
})
