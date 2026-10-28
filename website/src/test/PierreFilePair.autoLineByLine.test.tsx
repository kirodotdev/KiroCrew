/**
 * `PierreFilePair`'s `autoLineByLine` opt-in (#25237): an over-budget pair
 * starts its off-thread line-by-line diff on its own, through the same
 * computing → ready / error state machine the click uses.
 *
 * Harness: the REAL `pierre/index.tsx` and `pierre/PierreImpl.tsx` run; only
 * the library's imperative renderers and highlight pool are stubbed (they
 * cannot mount in jsdom). `Worker` is a fake that answers the diff worker's
 * protocol with the worker's OWN pure handler, so `diffOffThread` (its single
 * worker, its abort teardown and its LRU) is real. Two controls shape the
 * answer: `holdDiff` queues it until the test releases it, so the computing
 * state is observable for as long as a case needs; `failDiff` answers with an
 * error. `vi.resetModules()` runs before every case because the LRU is
 * module-level and a cache hit resolves before `holdDiff` could hold it.
 */
import { StrictMode, type ReactNode } from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileContents } from '@pierre/diffs'
import { PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE } from '../pierre/config'
import { handlePairDiffRequest, type PairDiffRequest, type PairDiffResponse } from '../pierre/diffWorker'

const state = vi.hoisted(() => ({
  /** Diffs that reached a worker. */
  computed: 0,
  /** Queue answers until `releaseDiffs()` instead of sending them at once. */
  holdDiff: false,
  /** Answer every request with an error. */
  failDiff: false,
  held: [] as Array<() => void>,
  /** Every diff worker `diffOffThread` built, in order. */
  diffWorkers: [] as Array<{ terminated: boolean }>,
}))

class FakeWorker {
  onmessage: ((e: MessageEvent<unknown>) => void) | null = null
  onerror: ((e: unknown) => void) | null = null
  terminated = false
  readonly isDiffWorker: boolean

  constructor(readonly url: URL | string) {
    this.isDiffWorker = String(url).includes('diffWorker')
    if (this.isDiffWorker) state.diffWorkers.push(this)
  }

  addEventListener() {}
  removeEventListener() {}

  postMessage(message: unknown) {
    if (!this.isDiffWorker) return
    state.computed++
    const request = message as PairDiffRequest
    const response: PairDiffResponse = state.failDiff
      ? { id: request.id, ok: false, error: 'boom' }
      : handlePairDiffRequest(request)
    const answer = () => {
      if (!this.terminated) this.onmessage?.({ data: response } as MessageEvent<unknown>)
    }
    if (state.holdDiff) state.held.push(answer)
    else void Promise.resolve().then(answer)
  }

  terminate() { this.terminated = true }
}

vi.mock('@pierre/diffs/worker', () => ({
  WorkerPoolManager: class {
    terminate = vi.fn()
    initialize() { return Promise.resolve() }
  },
}))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  return {
    File: () => <div data-testid="pierre-file" />,
    FileDiff: (props: { fileDiff?: { name?: string } }) => (
      <div data-testid="pierre-patch" data-name={props.fileDiff?.name}>hunks</div>
    ),
    MultiFileDiff: () => <div data-testid="pierre-pair" />,
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

const OVERSIZED = PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1
const lines = (count: number, patched = false) => Array.from(
  { length: count },
  (_, i) => (patched && i === 12 ? `const v${i} = ${i} /* patched */` : `const v${i} = ${i}`),
).join('\n')
const pair = (name: string, count = OVERSIZED): { oldFile: FileContents; newFile: FileContents } => ({
  oldFile: { name, contents: lines(count) },
  newFile: { name, contents: lines(count, true) },
})
const BIG = pair('big.ts')
const OTHER = pair('other.ts')
const SMALL = pair('small.ts', 20)
const OPTIONS = { collapsed: false, diffStyle: 'unified' as const, overflow: 'wrap' as const, disableFileHeader: false }

const SHOW = 'Show line-by-line diff'
const computingStrip = () => screen.queryByText('Computing line-by-line diff…')
/** The ready patch renders through `PairPatchImpl`, a `React.lazy` boundary over
 *  `import('./PierreImpl')` that `vi.resetModules()` makes every case load
 *  afresh; under shard load that chunk can take longer than the 1000ms
 *  default (website/docs/testing.md, "A `React.lazy` boundary races the 1000ms
 *  default"), so the wait gets an explicit timeout. */
const findPatch = () => screen.findByTestId('pierre-patch', {}, { timeout: 5000 })

async function loadPierreFilePair() {
  const { PierreFilePair } = await import('../pierre')
  return PierreFilePair
}

/** Send every held answer, then let the resulting state updates land. */
async function releaseDiffs() {
  const answers = state.held.splice(0)
  await act(async () => { for (const answer of answers) answer() })
}

beforeEach(() => {
  state.computed = 0
  state.holdDiff = false
  state.failDiff = false
  state.held.length = 0
  state.diffWorkers.length = 0
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('PierreFilePair autoLineByLine', () => {
  it('T2: starts the worker diff on its own and shows the computing strip on first paint', async () => {
    state.holdDiff = true
    const PierreFilePair = await loadPierreFilePair()
    render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)

    expect(computingStrip()).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: SHOW })).not.toBeInTheDocument()
    expect(state.computed).toBe(1)

    await releaseDiffs()
    expect(await findPatch()).toBeInTheDocument()
  })

  it('T3a: Cancel sticks for the same pair', async () => {
    state.holdDiff = true
    const user = userEvent.setup()
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(state.computed).toBe(1)

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: SHOW })).toBeInTheDocument()
    expect(computingStrip()).not.toBeInTheDocument()
    expect(state.diffWorkers[0].terminated).toBe(true)

    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(screen.getByRole('button', { name: SHOW })).toBeInTheDocument()
    expect(state.computed).toBe(1)
  })

  it('T3b: an error offers a manual retry and does not loop', async () => {
    state.failDiff = true
    const user = userEvent.setup()
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)

    expect(await screen.findByText('Could not compute the diff')).toBeInTheDocument()
    expect(state.computed).toBe(1)
    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(state.computed).toBe(1)

    state.failDiff = false
    await user.click(screen.getByRole('button', { name: SHOW }))
    expect(state.computed).toBe(2)
    expect(await findPatch()).toBeInTheDocument()
  })

  it('T5a: a pair change while computing aborts the old diff and computes the new pair', async () => {
    state.holdDiff = true
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(state.computed).toBe(1)

    rerender(<PierreFilePair {...OTHER} options={OPTIONS} autoLineByLine />)
    expect(state.diffWorkers[0].terminated).toBe(true)
    expect(state.computed).toBe(2)
    expect(computingStrip()).toBeInTheDocument()

    await releaseDiffs()
    expect(await findPatch()).toHaveAttribute('data-name', 'other.ts')
  })

  it('T5b: a pair change aborts a retry that replaced the auto diff', async () => {
    state.failDiff = true
    const user = userEvent.setup()
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(await screen.findByText('Could not compute the diff')).toBeInTheDocument()

    state.failDiff = false
    state.holdDiff = true
    await user.click(screen.getByRole('button', { name: SHOW }))
    expect(state.computed).toBe(2)
    const retryWorker = state.diffWorkers[state.diffWorkers.length - 1]
    expect(retryWorker.terminated).toBe(false)

    rerender(<PierreFilePair {...SMALL} options={OPTIONS} autoLineByLine />)
    expect(retryWorker.terminated).toBe(true)
    expect(computingStrip()).not.toBeInTheDocument()
  })

  it('T6: reaches the patch under StrictMode', async () => {
    const PierreFilePair = await loadPierreFilePair()
    render(
      <StrictMode>
        <PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />
      </StrictMode>,
    )
    // The mount-time cleanup aborts and clears the first request; the second
    // setup must start a fresh one that reaches the patch, not leave the
    // strip waiting on an aborted promise.
    expect(await findPatch()).toBeInTheDocument()
    expect(state.diffWorkers.at(-1)?.terminated).toBe(false)
  })

  it('TA: follows the prop on, then off', async () => {
    state.holdDiff = true
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine={false} />)
    expect(state.computed).toBe(0)
    expect(screen.getByRole('button', { name: SHOW })).toBeInTheDocument()

    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(state.computed).toBe(1)
    expect(computingStrip()).toBeInTheDocument()

    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine={false} />)
    expect(state.diffWorkers[0].terminated).toBe(true)
    expect(computingStrip()).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: SHOW })).toBeInTheDocument()
  })

  it('T7: a finished diff survives the tab going hidden and visible again', async () => {
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(await findPatch()).toBeInTheDocument()
    expect(state.computed).toBe(1)

    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine={false} />)
    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    // The ready branch keeps its patch: nothing new reached the worker.
    expect(screen.getByTestId('pierre-patch')).toBeInTheDocument()
    expect(state.computed).toBe(1)
  })

  it('T8: a failed diff waits for the retry button when the tab comes back', async () => {
    state.failDiff = true
    const PierreFilePair = await loadPierreFilePair()
    const { rerender } = render(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(await screen.findByText('Could not compute the diff')).toBeInTheDocument()

    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine={false} />)
    rerender(<PierreFilePair {...BIG} options={OPTIONS} autoLineByLine />)
    expect(state.computed).toBe(1)
    expect(screen.getByText('Could not compute the diff')).toBeInTheDocument()
    expect(computingStrip()).not.toBeInTheDocument()
  })

  it('T4: without the prop an over-budget pair still waits for the click', async () => {
    const PierreFilePair = await loadPierreFilePair()
    await act(async () => {
      render(<PierreFilePair {...BIG} options={OPTIONS} />)
    })
    expect(screen.getByRole('button', { name: SHOW })).toBeInTheDocument()
    expect(state.computed).toBe(0)
  })
})
