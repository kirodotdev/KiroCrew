// @vitest-environment happy-dom
import { act, render } from '@testing-library/react'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { WorkerPoolLifecycle, useWorkerPoolLifecycle, type WorkerPoolHandle } from '../pierre/workerPoolLifecycle'

interface FakePool { id: number }

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
  const scheduled: Array<{
    callback: () => void
    delayMs: number
    timer: ReturnType<typeof setTimeout>
    cancelled: boolean
  }> = []
  let nextTimer = 1
  const warn = vi.fn()
  const lifecycle = new WorkerPoolLifecycle<FakePool>({
    enabled: true,
    retryDelaysMs: [250, 1_000],
    cooldownMs: 30_000,
    stableAfterMs: 60_000,
    warn,
    schedule: (callback, delayMs) => {
      const timer = nextTimer++ as unknown as ReturnType<typeof setTimeout>
      scheduled.push({ callback, delayMs, timer, cancelled: false })
      return timer
    },
    cancel: timer => {
      const pending = scheduled.find(item => item.timer === timer)
      if (pending) pending.cancelled = true
    },
    create: (generation, reportFailure): WorkerPoolHandle<FakePool> => {
      const ready = deferred()
      const terminate = vi.fn()
      attempts.push({ generation, reportFailure, ready, terminate })
      return {
        pool: { id: generation },
        ready: ready.promise,
        terminate,
      }
    },
  })
  return { lifecycle, attempts, scheduled, warn }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Pierre worker pool lifecycle', () => {
  it('stays unavailable when workers are disabled', () => {
    const create = vi.fn()
    const lifecycle = new WorkerPoolLifecycle<FakePool>({
      enabled: false,
      create,
      retryDelaysMs: [250],
      cooldownMs: 30_000,
      stableAfterMs: 60_000,
    })

    lifecycle.start()
    expect(create).not.toHaveBeenCalled()
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'unavailable', generation: 0 })
  })

  it('stops a ready pool, terminates it, and publishes unavailable', async () => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()

    lifecycle.stop()
    expect(attempts[0].terminate).toHaveBeenCalledOnce()
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'unavailable', generation: 2 })
    lifecycle.start()
    expect(attempts).toHaveLength(1)
  })

  it('publishes ready only after initialization completes', async () => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'starting', generation: 1 })

    attempts[0].ready.resolve()
    await Promise.resolve()
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'ready', generation: 1, pool: { id: 1 } })
  })

  it('publishes plain-text recovery before terminating and replacing a failed pool', async () => {
    const { lifecycle, attempts, scheduled } = harness()
    const phases: string[] = []
    lifecycle.subscribe(() => phases.push(lifecycle.getSnapshot().phase))
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()

    attempts[0].reportFailure('boom')
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'recovering', generation: 1 })
    expect(attempts[0].terminate).not.toHaveBeenCalled()
    await Promise.resolve()
    expect(attempts[0].terminate).toHaveBeenCalledOnce()

    expect(scheduled).toHaveLength(1)
    expect(scheduled[0].delayMs).toBe(250)
    expect(attempts).toHaveLength(1)
    scheduled[0].callback()
    expect(attempts).toHaveLength(2)
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'recovering', generation: 2 })
    expect(phases).toContain('recovering')
  })

  it('ignores late failures from a retired generation', async () => {
    const { lifecycle, attempts, scheduled } = harness()
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()
    attempts[0].reportFailure('first')
    expect(scheduled[0].delayMs).toBe(250)
    scheduled[0].callback()
    attempts[1].ready.resolve()
    await Promise.resolve()

    attempts[0].reportFailure('late')
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'ready', generation: 2, pool: { id: 2 } })
    expect(attempts[1].terminate).not.toHaveBeenCalled()
  })

  it('bounds repeated initialization failures with a cooldown', async () => {
    const { lifecycle, attempts, scheduled } = harness()
    lifecycle.start()
    attempts[0].reportFailure(new Error('one'))
    expect(scheduled[0].delayMs).toBe(250)
    scheduled[0].callback()
    attempts[1].reportFailure(new Error('two'))
    expect(scheduled[1].delayMs).toBe(1_000)
    scheduled[1].callback()
    attempts[2].reportFailure(new Error('three'))

    expect(lifecycle.getSnapshot()).toEqual({ phase: 'cooldown', generation: 3 })
    expect(scheduled[2].delayMs).toBe(30_000)
    expect(attempts).toHaveLength(3)
    scheduled[2].callback()
    expect(attempts).toHaveLength(4)
    attempts[3].reportFailure(new Error('half-open failed'))
    expect(lifecycle.getSnapshot()).toEqual({ phase: 'unavailable', generation: 4 })
    expect(scheduled).toHaveLength(3)
  })


  it('preserves the failure budget until a replacement remains stable', async () => {
    const { lifecycle, attempts, scheduled } = harness()
    lifecycle.start()
    attempts[0].ready.resolve()
    await Promise.resolve()

    attempts[0].reportFailure('first')
    scheduled.find(item => item.delayMs === 250)!.callback()
    attempts[1].ready.resolve()
    await Promise.resolve()
    expect(scheduled.some(item => item.delayMs === 60_000 && !item.cancelled)).toBe(true)

    attempts[1].reportFailure('same failure after replacement')
    expect(scheduled.some(item => item.delayMs === 60_000 && item.cancelled)).toBe(true)
    expect(scheduled.some(item => item.delayMs === 1_000 && !item.cancelled)).toBe(true)
  })

  it('warns once through retries and cooldown, then resets after stability', async () => {
    const { lifecycle, attempts, scheduled, warn } = harness()
    lifecycle.start()
    attempts[0].reportFailure('one')
    scheduled[0].callback()
    attempts[1].reportFailure('two')
    scheduled[1].callback()
    attempts[2].reportFailure('three')
    expect(warn).toHaveBeenCalledTimes(1)

    scheduled[2].callback()
    attempts[3].ready.resolve()
    await Promise.resolve()
    scheduled.find(item => item.delayMs === 60_000 && !item.cancelled)!.callback()
    attempts[3].reportFailure('new episode')
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('re-renders subscribers and removes listeners on unmount', async () => {
    const { lifecycle, attempts } = harness()
    lifecycle.start()
    function Probe() {
      const state = useWorkerPoolLifecycle(lifecycle)
      return <span data-testid="phase">{state.phase}</span>
    }
    const view = render(<Probe />)
    expect(view.getByTestId('phase')).toHaveTextContent('starting')
    await act(async () => {
      attempts[0].ready.resolve()
      await Promise.resolve()
    })
    expect(view.getByTestId('phase')).toHaveTextContent('ready')
    view.unmount()
    expect(() => attempts[0].reportFailure('after unmount')).not.toThrow()
  })
})
