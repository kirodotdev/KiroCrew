// @vitest-environment happy-dom
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

const state = vi.hoisted(() => ({
  poolCalls: [] as Array<{ poolOptions: Record<string, unknown>; highlighterOptions: Record<string, unknown> }>,
  managers: [] as Array<{ workers: FakeWorker[]; terminate: ReturnType<typeof vi.fn> }>,
  componentProps: [] as Array<Record<string, unknown>>,
}))

class FakeWorker {
  listeners = new Map<string, Set<(event: unknown) => void>>()
  sent: unknown[] = []
  terminated = false

  addEventListener(type: string, listener: (event: unknown) => void) {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  postMessage(message: unknown) {
    this.sent.push(message)
  }

  terminate() {
    this.terminated = true
  }

  emit(type: string, event: unknown) {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

vi.mock('@pierre/diffs/worker', () => ({
  WorkerPoolManager: class {
    workers: FakeWorker[]
    terminate = vi.fn(() => {
      for (const worker of this.workers) worker.terminate()
    })

    constructor(poolOptions: { poolSize: number; workerFactory: () => FakeWorker }, highlighterOptions: Record<string, unknown>) {
      state.poolCalls.push({ poolOptions, highlighterOptions })
      this.workers = Array.from({ length: poolOptions.poolSize }, () => poolOptions.workerFactory())
      state.managers.push(this)
    }

    initialize() {
      return Promise.resolve()
    }
  },
}))

vi.mock('@pierre/diffs', () => ({
  EXTENSION_TO_FILE_FORMAT: {},
  parsePatchFiles: () => [{ files: [{ name: 'file.ts', hunks: [{}] }] }],
  setCustomExtension: () => {},
}))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  const record = (kind: string, props: Record<string, unknown>) => {
    state.componentProps.push(props)
    return <div data-testid={kind}>{kind}</div>
  }
  return {
    File: (props: Record<string, unknown>) => record('worker-file', props),
    FileDiff: (props: Record<string, unknown>) => record('worker-patch', props),
    MultiFileDiff: (props: Record<string, unknown>) => record('worker-pair', props),
    Virtualizer: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
    WorkerPoolContext: createContext<unknown>(undefined),
  }
})

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.resetModules()
  vi.stubGlobal('Worker', FakeWorker)
  state.poolCalls.length = 0
  state.managers.length = 0
  state.componentProps.length = 0
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

async function loadPierre() {
  const module = await import('../pierre/PierreImpl')
  await Promise.resolve()
  return module
}

describe('Pierre highlight worker pool recovery', () => {
  it('keeps the bounded WASM engine in highlighterOptions', async () => {
    await loadPierre()
    expect(state.poolCalls).toHaveLength(1)
    expect(state.poolCalls[0].highlighterOptions.preferredHighlighter).toBe('shiki-wasm')
    expect(state.poolCalls[0].poolOptions).not.toHaveProperty('preferredHighlighter')
  })

  it('switches mounted surfaces to complete plain text, terminates every worker, and remounts a replacement', async () => {
    const { PierreCodeImpl, PierrePatchImpl, PierreFilePairImpl } = await loadPierre()
    const view = render(<>
      <PierreCodeImpl file={{ name: 'code.ts', contents: 'CODE_FIRST\nCODE_LAST', lang: 'typescript' }} />
      <PierrePatchImpl patch={'--- a/file.ts\n+++ b/file.ts\n@@ -1 +1 @@\n-OLD_PATCH\n+NEW_PATCH'} />
      <PierreFilePairImpl
        oldFile={{ name: 'old.ts', contents: 'OLD_FIRST\nOLD_LAST' }}
        newFile={{ name: 'new.ts', contents: 'NEW_FIRST\nNEW_LAST' }}
      />
    </>)
    expect(view.getByTestId('worker-file')).toBeInTheDocument()
    expect(view.getByTestId('worker-patch')).toBeInTheDocument()
    expect(view.getByTestId('worker-pair')).toBeInTheDocument()

    await act(async () => {
      state.managers[0].workers[0].emit('error', { message: 'boom' })
      await Promise.resolve()
    })
    expect(view.queryByTestId('worker-file')).not.toBeInTheDocument()
    expect(view.queryByTestId('worker-patch')).not.toBeInTheDocument()
    expect(view.queryByTestId('worker-pair')).not.toBeInTheDocument()
    expect(view.container).toHaveTextContent('CODE_FIRST')
    expect(view.container).toHaveTextContent('CODE_LAST')
    expect(view.container).toHaveTextContent('OLD_PATCH')
    expect(view.container).toHaveTextContent('NEW_PATCH')
    expect(view.container).toHaveTextContent('OLD_FIRST')
    expect(view.container).toHaveTextContent('OLD_LAST')
    expect(view.container).toHaveTextContent('NEW_FIRST')
    expect(view.container).toHaveTextContent('NEW_LAST')
    expect(state.managers[0].workers.every(worker => worker.terminated)).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(250)
      await Promise.resolve()
    })
    expect(state.managers).toHaveLength(2)
    expect(view.getByTestId('worker-file')).toBeInTheDocument()
    expect(view.getByTestId('worker-patch')).toBeInTheDocument()
    expect(view.getByTestId('worker-pair')).toBeInTheDocument()
    expect(state.componentProps.every(props => !Object.hasOwn(props, 'disableWorkerPool'))).toBe(true)
  })


  it('keeps collapsed rows header-only, preserves filename click selectors, and honors disabled headers', async () => {
    const { PierreFilePairImpl } = await loadPierre()
    const oldFile = { name: 'file.ts', contents: 'OLD_CONTENT' }
    const newFile = { name: 'file.ts', contents: 'NEW_CONTENT' }
    const view = render(
      <PierreFilePairImpl oldFile={oldFile} newFile={newFile} options={{ collapsed: true, disableFileHeader: false }} />,
    )

    await act(async () => {
      state.managers[0].workers[0].emit('error', { message: 'boom' })
      await Promise.resolve()
    })
    const title = view.container.querySelector('[data-title]')
    expect(title).not.toBeNull()
    expect(title?.closest('[data-diffs-header]')).not.toBeNull()
    expect(view.container).not.toHaveTextContent('OLD_CONTENT')
    expect(view.container).not.toHaveTextContent('NEW_CONTENT')

    view.rerender(
      <PierreFilePairImpl oldFile={oldFile} newFile={newFile} options={{ collapsed: false, disableFileHeader: true }} />,
    )
    expect(view.container.querySelector('[data-diffs-header]')).toBeNull()
    expect(view.container).toHaveTextContent('OLD_CONTENT')
    expect(view.container).toHaveTextContent('NEW_CONTENT')

    view.rerender(<PierreFilePairImpl oldFile={oldFile} newFile={newFile} />)
    expect(view.container.querySelector('[data-diffs-header]')).toBeNull()
    expect(view.container).toHaveTextContent('OLD_CONTENT')
    expect(view.container).toHaveTextContent('NEW_CONTENT')
  })

  it('does not recycle the pool for a request-local protocol error', async () => {
    await loadPierre()
    state.managers[0].workers[0].emit('message', {
      data: { type: 'error', id: 'render-request', error: 'unsupported grammar input' },
    })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(state.managers).toHaveLength(1)
    expect(state.managers[0].terminate).not.toHaveBeenCalled()
  })
  it('recycles the pool when a worker request hangs', async () => {
    await loadPierre()
    const worker = state.managers[0].workers[0]
    worker.postMessage({ type: 'file', id: 'hung-request' })

    await vi.advanceTimersByTimeAsync(29_999)
    expect(state.managers[0].terminate).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    await Promise.resolve()
    expect(state.managers[0].terminate).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(250)
    expect(state.managers).toHaveLength(2)
  })

  it('ignores a stale worker failure after replacement', async () => {
    await loadPierre()
    const staleWorker = state.managers[0].workers[0]
    staleWorker.emit('messageerror', {})
    await Promise.resolve()
    await vi.advanceTimersByTimeAsync(250)
    await Promise.resolve()
    expect(state.managers).toHaveLength(2)

    staleWorker.emit('error', { message: 'late old error' })
    await vi.advanceTimersByTimeAsync(1_000)
    expect(state.managers).toHaveLength(2)
    expect(state.managers[1].terminate).not.toHaveBeenCalled()
  })
})
