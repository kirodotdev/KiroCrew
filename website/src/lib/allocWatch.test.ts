import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installAllocWatch, uninstallAllocWatch, DEFAULT_MIN_BYTES } from './allocWatch'

type Reporter = ReturnType<typeof vi.fn>

interface Scope {
  ArrayBuffer: typeof ArrayBuffer
  Uint8Array: typeof Uint8Array
  Float64Array: typeof Float64Array
  [k: string]: unknown
}

let reporter: Reporter
let scope: Scope

function freshScope(): Scope {
  return { ArrayBuffer, Uint8Array, Float64Array } as Scope
}

beforeEach(() => {
  reporter = vi.fn()
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = { reportBigAlloc: reporter }
  // Small threshold so tiny allocations trip it deterministically.
  ;(globalThis as unknown as { __KIROCREW_BIG_ALLOC_BYTES__?: number }).__KIROCREW_BIG_ALLOC_BYTES__ = 1024
  scope = freshScope()
})

afterEach(() => {
  uninstallAllocWatch()
  delete (window as unknown as { electronAPI?: unknown }).electronAPI
  delete (globalThis as unknown as { __KIROCREW_BIG_ALLOC_BYTES__?: number }).__KIROCREW_BIG_ALLOC_BYTES__
})

describe('installAllocWatch', () => {
  it('reports an ArrayBuffer allocation at or above the threshold', () => {
    installAllocWatch(scope)
    new scope.ArrayBuffer(2048)
    expect(reporter).toHaveBeenCalledTimes(1)
    const ev = reporter.mock.calls[0][0]
    expect(ev.kind).toBe('ArrayBuffer')
    expect(ev.bytes).toBe(2048)
    expect(ev.outcome).toBe('requested')
    expect(typeof ev.stack).toBe('string')
    // Its own frames are stripped, so the site is the caller, not the watcher.
    expect(ev.stack).not.toMatch(/allocWatch/)
  })

  it('does not report allocations below the threshold', () => {
    installAllocWatch(scope)
    new scope.ArrayBuffer(512)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('sizes typed arrays by element count times BYTES_PER_ELEMENT', () => {
    installAllocWatch(scope)
    new scope.Float64Array(300) // 300 * 8 = 2400 bytes >= 1024
    expect(reporter).toHaveBeenCalledTimes(1)
    const ev = reporter.mock.calls[0][0]
    expect(ev.kind).toBe('Float64Array')
    expect(ev.bytes).toBe(2400)
  })

  it('ignores a typed array constructed as a view over an existing buffer', () => {
    const buf = new ArrayBuffer(4096) // real global, pre-install
    installAllocWatch(scope)
    new scope.Uint8Array(buf) // first arg is a buffer, not a length: no fresh allocation
    expect(reporter).not.toHaveBeenCalled()
  })

  it('reports a failed allocation and rethrows', () => {
    installAllocWatch(scope)
    expect(() => new scope.ArrayBuffer(Number.MAX_SAFE_INTEGER)).toThrow()
    // one "requested" before, one "failed" after
    expect(reporter).toHaveBeenCalledTimes(2)
    expect(reporter.mock.calls[0][0].outcome).toBe('requested')
    const failed = reporter.mock.calls[1][0]
    expect(failed.outcome).toBe('failed')
    expect(failed.error).toMatch(/RangeError|Invalid|length/i)
  })

  it('preserves instanceof for wrapped constructors', () => {
    installAllocWatch(scope)
    const ab = new scope.ArrayBuffer(8)
    expect(ab instanceof ArrayBuffer).toBe(true)
    const ta = new scope.Uint8Array(8)
    expect(ta instanceof Uint8Array).toBe(true)
  })

  it('no-ops when electronAPI is absent (does not throw)', () => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    installAllocWatch(scope)
    expect(() => new scope.ArrayBuffer(2048)).not.toThrow()
    expect(reporter).not.toHaveBeenCalled()
  })

  it('is idempotent: a second install does not double-wrap', () => {
    installAllocWatch(scope)
    installAllocWatch(scope)
    new scope.ArrayBuffer(2048)
    expect(reporter).toHaveBeenCalledTimes(1)
  })

  it('uninstall restores the original constructors', () => {
    installAllocWatch(scope)
    uninstallAllocWatch()
    expect(scope.ArrayBuffer).toBe(ArrayBuffer)
    new scope.ArrayBuffer(2048)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('caps reports per session to bound IPC against a runaway allocator', () => {
    installAllocWatch(scope)
    for (let i = 0; i < 4100; i++) new scope.ArrayBuffer(2048)
    // MAX_REPORTS_PER_SESSION is 4096; anything past it is dropped.
    expect(reporter).toHaveBeenCalledTimes(4096)
  })

  it('exports a sane default threshold', () => {
    expect(DEFAULT_MIN_BYTES).toBe(64 * 1024 * 1024)
  })
})
