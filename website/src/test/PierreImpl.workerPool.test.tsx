/**
 * The highlight worker pool's ENGINE choice, pinned.
 *
 * `PierreImpl` builds the one worker pool the whole tab shares, and the engine
 * it asks for is the difference between a runaway grammar match being bounded
 * and killing the renderer process. Pierre's own default (`shiki-js`) runs
 * TextMate patterns on V8's RegExp with no per-match ceiling; six renderer
 * deaths on 2026-08-30 were V8 cage OOMs raised from `tokenizeLine2` ->
 * `findNextMatchSync` -> `EmulatedRegExp.exec` with a JS heap of 8-32 MB out of
 * 4192 MB. `shiki-wasm` is the reference oniguruma build and aborts a
 * pathological match at its compiled-in retry limit instead.
 *
 * The option is easy to lose and impossible to notice losing: it lands in
 * `highlighterOptions`, NOT `poolOptions`, and Pierre silently defaults an
 * absent value back to `shiki-js`. Nothing else in the suite reads it, so this
 * file is the guard. Deleting the option from `PierreImpl` fails the first
 * assertion; moving it to `poolOptions` fails the second.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'

const pool = vi.hoisted(() => ({
  calls: [] as { poolOptions: Record<string, unknown>; highlighterOptions: Record<string, unknown> }[],
}))

vi.mock('@pierre/diffs/worker', () => ({
  getOrCreateWorkerPoolSingleton: (args: {
    poolOptions: Record<string, unknown>
    highlighterOptions: Record<string, unknown>
  }) => {
    pool.calls.push(args)
    return { isWorkingPool: () => true }
  },
}))

// The library itself is never exercised here — only the options the wrapper
// hands its pool — so these doubles carry just enough shape to let the module
// evaluate under happy-dom.
vi.mock('@pierre/diffs', () => ({
  EXTENSION_TO_FILE_FORMAT: {},
  parsePatchFiles: () => [],
  setCustomExtension: () => {},
}))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  return {
    File: () => null,
    FileDiff: () => null,
    MultiFileDiff: () => null,
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

describe('Pierre highlight worker pool engine', () => {
  beforeEach(() => {
    pool.calls.length = 0
    vi.resetModules()
    // The pool is only constructed when a Worker constructor exists — without
    // this the module takes its SSR branch and makes no call to assert on.
    vi.stubGlobal('Worker', class {})
  })

  it('asks for the WASM oniguruma engine, whose pathological matches are bounded', async () => {
    await import('../pierre/PierreImpl')

    expect(pool.calls).toHaveLength(1)
    expect(pool.calls[0].highlighterOptions.preferredHighlighter).toBe('shiki-wasm')
  })

  it('passes the engine in highlighterOptions, the only argument Pierre reads it from', async () => {
    await import('../pierre/PierreImpl')

    // WorkerPoolManager takes the engine off its SECOND argument; a value on
    // poolOptions is silently ignored and the default `shiki-js` stands.
    expect(pool.calls[0].poolOptions).not.toHaveProperty('preferredHighlighter')
    expect(Object.keys(pool.calls[0].highlighterOptions)).toContain('preferredHighlighter')
  })
})
