// @vitest-environment happy-dom
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import type { WorkerPoolManager } from '@pierre/diffs/worker'
import { WorkerPoolLifecycle, type WorkerPoolHandle } from '../pierre/workerPoolLifecycle'


function deferred() {
  let resolve!: () => void
  let reject!: (error: unknown) => void
  const promise = new Promise<void>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function harness() {
  const attempts: Array<{
    generation: number
    reportFailure: (reason?: unknown) => void
    ready: ReturnType<typeof deferred>
    terminate: ReturnType<typeof vi.fn>
  }> = []
  const onUnavailable = vi.fn()
  const lifecycle = new WorkerPoolLifecycle({
    retryDelaysMs: [250, 1_000],
    cooldownMs: 30_000,
    stableAfterMs: 60_000,
    onUnavailable,
    create: (reportFailure): WorkerPoolHandle => {
      const generation = attempts.length + 1
      const ready = deferred()
      const terminate = vi.fn()
      attempts.push({ generation, reportFailure, ready, terminate })
      return {
        pool: { id: generation } as unknown as WorkerPoolManager,
        ready: ready.promise,
        terminate,
      }
    },
  })
  return { lifecycle, attempts, onUnavailable }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Pierre worker pool lifecycle', () => {
  it('publishes ready only after initialization completes', async () => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'starting', generation: 1, pool: { id: 1 } })

    attempts[0].ready.resolve()
    await Promise.resolve()
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'ready', generation: 1, pool: { id: 1 } })
  })

  it('publishes plain-text recovery before terminating and replacing a failed pool', async () => {
    const { lifecycle, attempts } = harness()
    const phases: string[] = []
    lifecycle.subscribe(() => phases.push(lifecycle.getSnapshot().phase))
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()

    attempts[0].reportFailure('boom')
    expect(lifecycle.getSnapshot()).toEqual({
      phase: 'recovering',
      generation: 1,
      failure: { classification: 'error', message: 'boom', generation: 1, attempt: 1 },
    })
    expect(attempts[0].terminate).not.toHaveBeenCalled()
    // Subscribers commit in microtasks; termination must outlast all of them.
    await Promise.resolve()
    await Promise.resolve()
    expect(attempts[0].terminate).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(0)
    expect(attempts[0].terminate).toHaveBeenCalledOnce()

    expect(attempts).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(249)
    expect(attempts).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(attempts).toHaveLength(2)
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'recovering', generation: 2 })
    expect(phases).toContain('recovering')
  })

  it('ignores late failures from a retired generation', async () => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()
    attempts[0].reportFailure('first')
    await vi.advanceTimersByTimeAsync(250)
    attempts[1].ready.resolve()
    await Promise.resolve()

    attempts[0].reportFailure('late')
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'ready', generation: 2, pool: { id: 2 } })
    expect(attempts[1].terminate).not.toHaveBeenCalled()
  })

  it('bounds repeated initialization failures with a cooldown', async () => {
    const { lifecycle, attempts, onUnavailable } = harness()
    lifecycle.start()
    attempts[0].reportFailure(new Error('one'))
    await vi.advanceTimersByTimeAsync(250)
    attempts[1].reportFailure(new Error('two'))
    await vi.advanceTimersByTimeAsync(1_000)
    attempts[2].reportFailure(new Error('three'))

    expect(lifecycle.getSnapshot()).toEqual({
      phase: 'recovering',
      generation: 3,
      failure: { classification: 'error', message: 'three', generation: 3, attempt: 3 },
    })
    expect(attempts).toHaveLength(3)
    expect(onUnavailable).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(29_999)
    expect(attempts).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(attempts).toHaveLength(4)
    attempts[3].reportFailure(new Error('half-open failed'))
    const terminal = {
      classification: 'error',
      message: 'half-open failed',
      generation: 4,
      attempt: 4,
    }
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'unavailable', generation: 4, failure: terminal })
    expect(onUnavailable).toHaveBeenCalledOnce()
    expect(onUnavailable).toHaveBeenCalledWith(terminal)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(attempts).toHaveLength(4)
  })

  it.each([
    ['error', 'worker failed'],
    ['messageerror', 'worker message could not be deserialized'],
    ['postMessage throw', 'DataCloneError'],
    ['init timeout', 'worker initialization timed out'],
  ] as const)('records %s with its generation and attempt', (classification, message) => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    attempts[0].reportFailure({ classification, message })
    expect(lifecycle.getSnapshot()).toEqual({
      phase: 'recovering',
      generation: 1,
      failure: { classification, message, generation: 1, attempt: 1 },
    })
  })

  it('preserves the failure budget until a replacement remains stable', async () => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()

    attempts[0].reportFailure('first')
    await vi.advanceTimersByTimeAsync(250)
    attempts[1].ready.resolve()
    await Promise.resolve()

    attempts[1].reportFailure('same failure after replacement')
    await vi.advanceTimersByTimeAsync(999)
    expect(attempts).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(attempts).toHaveLength(3)
  })

  it('publishes to subscribers and removes listeners on unsubscribe', async () => {
    const { lifecycle, attempts } = harness()
    const phases: string[] = []
    const unsubscribe = lifecycle.subscribe(() => phases.push(lifecycle.getSnapshot().phase))
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()
    expect(phases).toEqual(['starting', 'starting', 'ready'])

    unsubscribe()
    attempts[0].reportFailure('after unsubscribe')
    expect(phases).toEqual(['starting', 'starting', 'ready'])
  })

})
