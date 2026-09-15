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

/** Props the diff surface handed the library, so the plain-mode assertions can
 *  read the two things that make the saving real. */
const diffProps = vi.hoisted(() => ({ last: undefined as Record<string, unknown> | undefined }))

vi.mock('@pierre/diffs/react', async () => {
  const { createContext } = await import('react')
  const record = (kind: string, props: Record<string, unknown>) => {
    state.componentProps.push(props)
    return <div data-testid={kind}>{kind}</div>
  }
  return {
    File: (props: Record<string, unknown>) => record('worker-file', props),
    FileDiff: (props: Record<string, unknown>) => record('worker-patch', props),
    MultiFileDiff: (props: Record<string, unknown>) => {
      diffProps.last = props
      return record('worker-pair', props)
    },
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
  diffProps.last = undefined
  localStorage.clear()
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

const FILE = { name: 'app.ts', contents: 'const a = 1\n' }

async function startPool() {
  const module = await loadPierre()
  let view!: ReturnType<typeof render>
  await act(async () => {
    view = render(<module.PierreCodeImpl file={FILE} />)
    await Promise.resolve()
    await Promise.resolve()
  })
  return { module, view }
}

describe('Pierre highlight worker pool recovery', () => {
  it('keeps the bounded WASM engine in highlighterOptions', async () => {
    await startPool()
    expect(state.poolCalls).toHaveLength(1)
    expect(state.poolCalls[0].highlighterOptions.preferredHighlighter).toBe('shiki-wasm')
    expect(state.poolCalls[0].poolOptions).not.toHaveProperty('preferredHighlighter')
  })

  it('renders Pierre on the main thread when the Worker API is absent', async () => {
    vi.stubGlobal('Worker', undefined)
    const module = await loadPierre()
    const view = render(<module.PierreCodeImpl file={FILE} />)
    await act(async () => { await Promise.resolve() })

    expect(state.poolCalls).toHaveLength(0)
    expect(view.getByTestId('worker-file')).toBeInTheDocument()
    expect(view.queryByRole('alert')).toBeNull()
    expect(view.queryByText(FILE.contents.trim(), { selector: 'pre' })).toBeNull()
  })

  it('stays silent while an editor surface is mounted and returns once it unmounts', async () => {
    const { module, view } = await startPool()
    // What `PierreEditorImpl` does on mount, without its Pierre edit imports.
    function EditorSurface() {
      module.useRegisterEditorSurface()
      return <textarea aria-label="draft" />
    }
    view.rerender(<>
      <module.PierreCodeImpl file={FILE} />
      <EditorSurface />
    </>)
    await act(async () => {
      state.managers[0].workers[0].emit('error', { message: 'one' })
      await vi.advanceTimersByTimeAsync(250)
      state.managers[1].workers[0].emit('error', { message: 'two' })
      await vi.advanceTimersByTimeAsync(1_000)
      state.managers[2].workers[0].emit('error', { message: 'three' })
      await vi.advanceTimersByTimeAsync(30_000)
      state.managers[3].workers[0].emit('error', { message: 'half-open failed' })
      await Promise.resolve()
    })
    // The editor shows its own save-first notice where the draft lives; a
    // tab-wide "reload" instruction would risk a draft the reader cannot see.
    expect(view.queryByRole('alert')).toBeNull()
    expect(view.queryByRole('button', { name: 'Ask the agent' })).toBeNull()

    view.rerender(<module.PierreCodeImpl file={FILE} />)
    expect(view.getByRole('alert')).toHaveClass('fixed')
    expect(view.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('shows one terminal hand-off across passive surfaces', async () => {
    const { module, view } = await startPool()
    view.rerender(<>
      <module.PierreCodeImpl file={FILE} />
      <module.PierreCodeImpl file={{ ...FILE, name: 'second.ts' }} />
    </>)

    await act(async () => {
      state.managers[0].workers[0].emit('error', { message: 'one' })
      await vi.advanceTimersByTimeAsync(250)
      state.managers[1].workers[0].emit('error', { message: 'two' })
      await vi.advanceTimersByTimeAsync(1_000)
      state.managers[2].workers[0].emit('error', { message: 'three' })
      await vi.advanceTimersByTimeAsync(30_000)
      state.managers[3].workers[0].emit('error', { message: 'half-open failed' })
      await Promise.resolve()
    })

    expect(view.getAllByRole('alert')).toHaveLength(1)
    expect(view.getByRole('alert')).toHaveClass('fixed')
    expect(view.getByRole('alert')).toHaveTextContent(
      'Syntax highlighting is unavailable until you reload. Content remains readable.',
    )
    expect(view.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()

    // Terminal is forever, so the notice is dismissible — once, for every
    // surface in the tab, including ones mounted afterwards.
    await act(async () => {
      view.getByRole('button', { name: 'Dismiss' }).click()
    })
    expect(view.queryByRole('alert')).toBeNull()
    view.rerender(<>
      <module.PierreCodeImpl file={FILE} />
      <module.PierreCodeImpl file={{ ...FILE, name: 'second.ts' }} />
      <module.PierreCodeImpl file={{ ...FILE, name: 'third.ts' }} />
    </>)
    expect(view.queryByRole('alert')).toBeNull()
  })

  it('switches mounted surfaces to complete plain text, terminates every worker, and remounts a replacement', async () => {
    const { PierreCodeImpl, PierrePatchImpl, PierreFilePairImpl } = await loadPierre()
    let view!: ReturnType<typeof render>
    await act(async () => {
      view = render(<>
        <PierreCodeImpl file={{ name: 'code.ts', contents: 'CODE_FIRST\nCODE_LAST', lang: 'typescript' }} />
        <PierrePatchImpl patch={'--- a/file.ts\n+++ b/file.ts\n@@ -1 +1 @@\n-OLD_PATCH\n+NEW_PATCH'} />
        <PierreFilePairImpl
          oldFile={{ name: 'old.ts', contents: 'OLD_FIRST\nOLD_LAST' }}
          newFile={{ name: 'new.ts', contents: 'NEW_FIRST\nNEW_LAST' }}
          fallbackClassName="archived-bound"
          fallbackContentStyle={{ maxHeight: '120px' }}
        />
      </>)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(view.getByTestId('worker-file')).toBeInTheDocument()
    expect(view.getByTestId('worker-patch')).toBeInTheDocument()
    expect(view.getByTestId('worker-pair')).toBeInTheDocument()

    await act(async () => {
      state.managers[0].workers[0].emit('error', { message: 'boom' })
      await Promise.resolve()
    })
    // Surfaces leave Pierre on the publish; workers die one macrotask later.
    expect(state.managers[0].terminate).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
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
    expect(view.container.querySelector('pre.archived-bound')).toHaveStyle({ maxHeight: '120px' })
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

  it('keeps header actions out of fallbacks (max-two-buttons-per-row)', async () => {
    const { PierrePatchImpl, PierreFilePairImpl } = await loadPierre()
    const actions = () => <><button>Open</button><button>Layout</button><button>Copy</button></>
    const view = render(<>
      <PierrePatchImpl patch={'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'} renderHeaderMetadata={actions} />
      <PierreFilePairImpl
        oldFile={{ name: 'x.ts', contents: 'OLD' }}
        newFile={{ name: 'x.ts', contents: 'NEW' }}
        options={{ disableFileHeader: false }}
        renderHeaderMetadata={actions}
      />
    </>)
    await act(async () => {
      state.managers[0].workers[0].emit('error', { message: 'boom' })
      await Promise.resolve()
    })
    expect(view.queryByTestId('worker-patch')).toBeNull()
    expect(view.queryByTestId('worker-pair')).toBeNull()
    expect(view.container.querySelector('[data-title]')).not.toBeNull()
    expect(view.queryAllByRole('button')).toHaveLength(0)
  })

  it('does not recycle the pool for a request-local protocol error', async () => {
    await startPool()
    state.managers[0].workers[0].emit('message', {
      data: { type: 'error', id: 'render-request', error: 'unsupported grammar input' },
    })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(state.managers).toHaveLength(1)
    expect(state.managers[0].terminate).not.toHaveBeenCalled()
  })
  it('gives worker initialization a wider timeout budget', async () => {
    await startPool()
    const worker = state.managers[0].workers[0]
    worker.postMessage({ type: 'initialize', id: 'slow-initialize' })

    await vi.advanceTimersByTimeAsync(119_999)
    expect(state.managers[0].terminate).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    // The watchdog schedules termination one macrotask later; fake timers stamp
    // a timer created inside a tick at now + 1.
    await vi.advanceTimersByTimeAsync(1)
    expect(state.managers[0].terminate).toHaveBeenCalledOnce()
  })


  it('recycles the pool when a worker request hangs', async () => {
    await startPool()
    const worker = state.managers[0].workers[0]
    worker.postMessage({ type: 'file', id: 'hung-request' })

    await vi.advanceTimersByTimeAsync(29_999)
    expect(state.managers[0].terminate).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    // The watchdog schedules termination one macrotask later; fake timers stamp
    // a timer created inside a tick at now + 1.
    await vi.advanceTimersByTimeAsync(1)
    expect(state.managers[0].terminate).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(250)
    expect(state.managers).toHaveLength(2)
  })

  it('ignores a stale worker failure after replacement', async () => {
    await startPool()
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

  it('builds nothing merely by loading the module', async () => {
    // The lazy chunk is reachable for reasons that never highlight — a preload,
    // a test warming it, a surface that unmounts before it paints. Each worker
    // costs a highlighter bundle and a WASM instantiation, so none is spawned
    // until something asks.
    await loadPierre()

    expect(state.poolCalls).toHaveLength(0)
  })

  it('builds ONE pool however many surfaces mount', async () => {
    const { PierreCodeImpl } = await loadPierre()
    await act(async () => {
      render(<PierreCodeImpl file={FILE} />)
      render(<PierreCodeImpl file={{ ...FILE, name: 'other.ts' }} />)
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(state.poolCalls).toHaveLength(1)
  })

  describe('plain-diff mode on the file-pair surface', () => {
    it('uses the text grammar without bypassing the worker lifecycle', async () => {
      localStorage.setItem('mc-diff-plain', '1')
      const { PierreFilePairImpl } = await loadPierre()
      await act(async () => {
        render(<PierreFilePairImpl oldFile={FILE} newFile={{ ...FILE, contents: 'const a = 2\n' }} />)
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(state.poolCalls).toHaveLength(0)
      expect(diffProps.last).not.toHaveProperty('disableWorkerPool')
      expect((diffProps.last?.oldFile as { lang?: string }).lang).toBe('text')
      expect((diffProps.last?.newFile as { lang?: string }).lang).toBe('text')
    })

    it('keys the cache by mode, so a live toggle cannot serve the other render’s tokens', async () => {
      localStorage.setItem('mc-diff-plain', '1')
      const { PierreFilePairImpl } = await loadPierre()
      let plainView!: ReturnType<typeof render>
      await act(async () => {
        plainView = render(<PierreFilePairImpl oldFile={FILE} newFile={FILE} />)
        await Promise.resolve()
      })
      const plainKey = (diffProps.last?.newFile as { cacheKey?: string }).cacheKey

      plainView.unmount()
      localStorage.clear()
      await act(async () => {
        render(<PierreFilePairImpl oldFile={FILE} newFile={FILE} />)
        await Promise.resolve()
        await Promise.resolve()
      })
      const colouredKey = (diffProps.last?.newFile as { cacheKey?: string }).cacheKey

      // Pierre caches tokens by cacheKey; identical keys would paint the plain
      // render with the coloured one's cached tokens (and the reverse).
      expect(plainKey).not.toBe(colouredKey)
    })

    it('highlights with the shared pool when the preference is unset', async () => {
      const { PierreFilePairImpl } = await loadPierre()
      await act(async () => {
        render(<PierreFilePairImpl oldFile={FILE} newFile={{ ...FILE, contents: 'const a = 2\n' }} />)
        await Promise.resolve()
        await Promise.resolve()
      })

      expect(state.poolCalls).toHaveLength(1)
      expect(diffProps.last).not.toHaveProperty('disableWorkerPool')
      expect((diffProps.last?.newFile as { lang?: string }).lang).toBeUndefined()
    })
  })
})
