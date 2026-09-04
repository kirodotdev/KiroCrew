/**
 * The highlight worker pool's ENGINE choice and its ON-DEMAND construction,
 * pinned.
 *
 * Two independent guards live here because they fail the same silent way — a
 * regression costs performance or stability, never a visible error.
 *
 * The pool is built by the first surface that intends to highlight, NOT at module
 * scope, because every worker spawns eagerly at init and loads its own
 * highlighter bundle plus the WASM regex engine. Moving the call back to module
 * scope would charge that to anyone who merely loaded the chunk, and would undo
 * plain-diff mode's saving entirely: the surfaces that want no colour opt out via
 * `disabled`, and opting out only means anything while the pool is lazy.
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
import { render } from '@testing-library/react'
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

/** Props the diff surface handed the library, so the plain-mode assertions can
 *  read the two things that make the saving real. */
const diffProps = vi.hoisted(() => ({ last: undefined as Record<string, unknown> | undefined }))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  return {
    File: () => null,
    FileDiff: () => null,
    MultiFileDiff: (props: Record<string, unknown>) => {
      diffProps.last = props
      return null
    },
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

/** A fresh copy of the module, whose lazily-built pool has not been resolved yet
 *  (`vi.resetModules` in beforeEach clears the memo). */
const loadPierre = () => import('../pierre/PierreImpl')

const FILE = { name: 'app.ts', contents: 'const a = 1\n' }

describe('Pierre highlight worker pool', () => {
  beforeEach(() => {
    pool.calls.length = 0
    diffProps.last = undefined
    localStorage.clear()
    vi.resetModules()
    // The pool is only constructed when a Worker constructor exists — without
    // this the module takes its SSR branch and makes no call to assert on.
    vi.stubGlobal('Worker', class {})
  })

  it('asks for the WASM oniguruma engine, whose pathological matches are bounded', async () => {
    const { PierreShell } = await loadPierre()
    render(<PierreShell>surface</PierreShell>)

    expect(pool.calls).toHaveLength(1)
    expect(pool.calls[0].highlighterOptions.preferredHighlighter).toBe('shiki-wasm')
  })

  it('passes the engine in highlighterOptions, the only argument Pierre reads it from', async () => {
    const { PierreShell } = await loadPierre()
    render(<PierreShell>surface</PierreShell>)

    // WorkerPoolManager takes the engine off its SECOND argument; a value on
    // poolOptions is silently ignored and the default `shiki-js` stands.
    expect(pool.calls[0].poolOptions).not.toHaveProperty('preferredHighlighter')
    expect(Object.keys(pool.calls[0].highlighterOptions)).toContain('preferredHighlighter')
  })

  it('builds nothing merely by loading the module', async () => {
    // The lazy chunk is reachable for reasons that never highlight — a preload,
    // a test warming it, a surface that unmounts before it paints. Each worker
    // costs a highlighter bundle and a WASM instantiation, so none is spawned
    // until something asks.
    await loadPierre()

    expect(pool.calls).toHaveLength(0)
  })

  it('builds ONE pool however many surfaces mount', async () => {
    // The whole tab shares it, and the memo is what keeps a virtualized list of
    // chat diffs from spawning a pool per block.
    const { PierreShell } = await loadPierre()
    render(<PierreShell>a</PierreShell>)
    render(<PierreShell>b</PierreShell>)

    expect(pool.calls).toHaveLength(1)
  })

  it('builds nothing for a shell that opted out', async () => {
    // `disabled` is the plain-diff and broken-pool path. It has to PREVENT
    // construction, not just withhold the context value — otherwise the workers
    // spawn and idle, which is the cost the preference exists to remove.
    const { PierreShell } = await loadPierre()
    render(<PierreShell disabled>surface</PierreShell>)

    expect(pool.calls).toHaveLength(0)
  })

  describe('plain-diff mode on the file-pair surface', () => {
    it('spawns no workers and declares both sides plain text', async () => {
      localStorage.setItem('mc-diff-plain', '1')
      const { PierreFilePairImpl } = await loadPierre()
      render(<PierreFilePairImpl oldFile={FILE} newFile={{ ...FILE, contents: 'const a = 2\n' }} />)

      // The saving: no pool, so no worker ever spawns for this surface.
      expect(pool.calls).toHaveLength(0)
      // And nothing for one to do anyway — `text` is Pierre's plaintext grammar,
      // which is what removes the colour while keeping rows and gutters.
      expect(diffProps.last?.disableWorkerPool).toBe(true)
      expect((diffProps.last?.oldFile as { lang?: string }).lang).toBe('text')
      expect((diffProps.last?.newFile as { lang?: string }).lang).toBe('text')
    })

    it('keys the cache by mode, so a live toggle cannot serve the other render’s tokens', async () => {
      localStorage.setItem('mc-diff-plain', '1')
      const { PierreFilePairImpl } = await loadPierre()
      render(<PierreFilePairImpl oldFile={FILE} newFile={FILE} />)
      const plainKey = (diffProps.last?.newFile as { cacheKey?: string }).cacheKey

      localStorage.clear()
      render(<PierreFilePairImpl oldFile={FILE} newFile={FILE} />)
      const colouredKey = (diffProps.last?.newFile as { cacheKey?: string }).cacheKey

      // Pierre caches tokens by cacheKey; identical keys would paint the plain
      // render with the coloured one's cached tokens (and the reverse).
      expect(plainKey).not.toBe(colouredKey)
    })

    it('highlights with the shared pool when the preference is unset', async () => {
      const { PierreFilePairImpl } = await loadPierre()
      render(<PierreFilePairImpl oldFile={FILE} newFile={{ ...FILE, contents: 'const a = 2\n' }} />)

      expect(pool.calls).toHaveLength(1)
      expect(diffProps.last?.disableWorkerPool).toBe(false)
      expect((diffProps.last?.newFile as { lang?: string }).lang).toBeUndefined()
    })
  })
})
