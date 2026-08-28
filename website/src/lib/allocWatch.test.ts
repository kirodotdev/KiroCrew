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

// Small threshold so tiny allocations trip it deterministically.
const TEST_MIN_BYTES = 1024

beforeEach(() => {
  reporter = vi.fn()
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = { reportBigAlloc: reporter }
  scope = freshScope()
})

afterEach(() => {
  uninstallAllocWatch()
  delete (window as unknown as { electronAPI?: unknown }).electronAPI
})

describe('installAllocWatch', () => {
  it('reports an ArrayBuffer allocation at or above the threshold', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    new scope.ArrayBuffer(2048)
    expect(reporter).toHaveBeenCalledTimes(1)
    const ev = reporter.mock.calls[0][0]
    expect(ev.kind).toBe('ArrayBuffer')
    expect(ev.bytes).toBe(2048)
    expect(ev.outcome).toBe('requested')
    expect(typeof ev.stack).toBe('string')
    // Its own frames are stripped, so the site is the caller, not the watcher.
    expect(ev.stack).not.toMatch(/allocWatch\.ts/)
  })

  it('leads the reported stack with the caller, not watcher machinery', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    function allocSiteForStackTest(): ArrayBuffer {
      return new scope.ArrayBuffer(2048)
    }
    allocSiteForStackTest()
    const ev = reporter.mock.calls[0][0]
    // The trim is anchored on the construct trap by FUNCTION REFERENCE
    // (Error.captureStackTrace), so it holds even when a minifier renames every
    // identifier in the watcher module: the first frame is the allocation site.
    const firstFrame = String(ev.stack).split(' <- ')[0]
    expect(firstFrame).toContain('allocSiteForStackTest')
  })

  it('does not report allocations below the threshold', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    new scope.ArrayBuffer(512)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('uses the 64 MiB default threshold when no minBytes option is passed', () => {
    // Allocation-free stub: requestedBytes reads only args[0] and
    // BYTES_PER_ELEMENT, so the boundary is provable without materializing two
    // real 64 MiB backing stores inside a cage-sensitive test runner.
    function ArrayBufferStub(this: unknown, _n: number) {}
    scope.ArrayBuffer = ArrayBufferStub as unknown as typeof ArrayBuffer
    installAllocWatch(scope)
    new (scope.ArrayBuffer as typeof ArrayBuffer)(DEFAULT_MIN_BYTES - 1)
    expect(reporter).not.toHaveBeenCalled()
    new (scope.ArrayBuffer as typeof ArrayBuffer)(DEFAULT_MIN_BYTES)
    expect(reporter).toHaveBeenCalledTimes(1)
  })

  it('ignores a non-positive minBytes option and keeps the default', () => {
    installAllocWatch(scope, { minBytes: -1 })
    new scope.ArrayBuffer(2048)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('ignores a non-finite minBytes option and keeps the default', () => {
    // Infinity would silently disable all reporting; it must fall back to the
    // default threshold instead. Stubbed for the same reason as above.
    function ArrayBufferStub(this: unknown, _n: number) {}
    scope.ArrayBuffer = ArrayBufferStub as unknown as typeof ArrayBuffer
    installAllocWatch(scope, { minBytes: Infinity })
    new (scope.ArrayBuffer as typeof ArrayBuffer)(DEFAULT_MIN_BYTES)
    expect(reporter).toHaveBeenCalledTimes(1)
  })

  it('sizes typed arrays by element count times BYTES_PER_ELEMENT', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    new scope.Float64Array(300) // 300 * 8 = 2400 bytes >= 1024
    expect(reporter).toHaveBeenCalledTimes(1)
    const ev = reporter.mock.calls[0][0]
    expect(ev.kind).toBe('Float64Array')
    expect(ev.bytes).toBe(2400)
  })

  it('ignores a typed array constructed as a view over an existing buffer', () => {
    const buf = new ArrayBuffer(4096) // real global, pre-install
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    new scope.Uint8Array(buf) // first arg is a buffer, not a length: no fresh allocation
    expect(reporter).not.toHaveBeenCalled()
  })

  it('reports a failed allocation and rethrows', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    expect(() => new scope.ArrayBuffer(Number.MAX_SAFE_INTEGER)).toThrow()
    // one "requested" before, one "failed" after
    expect(reporter).toHaveBeenCalledTimes(2)
    expect(reporter.mock.calls[0][0].outcome).toBe('requested')
    const failed = reporter.mock.calls[1][0]
    expect(failed.outcome).toBe('failed')
    expect(failed.error).toMatch(/RangeError|Invalid|length/i)
  })

  it('preserves instanceof for wrapped constructors', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    const ab = new scope.ArrayBuffer(8)
    expect(ab instanceof ArrayBuffer).toBe(true)
    const ta = new scope.Uint8Array(8)
    expect(ta instanceof Uint8Array).toBe(true)
  })

  it('returns early when electronAPI is absent, leaving constructors pristine', () => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    // A plain-browser dashboard can never report, so it must not pay for (or
    // carry the identity caveats of) patched constructors.
    expect(scope.ArrayBuffer).toBe(ArrayBuffer)
    expect(scope.Uint8Array).toBe(Uint8Array)
    expect(() => new scope.ArrayBuffer(2048)).not.toThrow()
    expect(reporter).not.toHaveBeenCalled()
  })

  it('returns early when electronAPI exists without reportBigAlloc', () => {
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = {}
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    expect(scope.ArrayBuffer).toBe(ArrayBuffer)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('is idempotent: a second install does not double-wrap', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    new scope.ArrayBuffer(2048)
    expect(reporter).toHaveBeenCalledTimes(1)
  })

  it('uninstall restores the original constructors', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    uninstallAllocWatch()
    expect(scope.ArrayBuffer).toBe(ArrayBuffer)
    new scope.ArrayBuffer(2048)
    expect(reporter).not.toHaveBeenCalled()
  })

  it('caps reports per session to bound IPC against a runaway allocator', () => {
    installAllocWatch(scope, { minBytes: TEST_MIN_BYTES })
    for (let i = 0; i < 4100; i++) new scope.ArrayBuffer(2048)
    // MAX_REPORTS_PER_SESSION is 4096; anything past it is dropped.
    expect(reporter).toHaveBeenCalledTimes(4096)
  })

  it('exports a sane default threshold', () => {
    expect(DEFAULT_MIN_BYTES).toBe(64 * 1024 * 1024)
  })
})
