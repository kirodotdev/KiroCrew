// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkerPoolManager } from '@pierre/diffs/worker'
import { createMonitoredWorker } from '../pierre/PierreImpl'
import {
  PIERRE_REGEX_ENGINE,
  PIERRE_THEMES,
  PIERRE_WORKER_INITIALIZATION_TIMEOUT_MS,
} from '../pierre/config'

const protocol = { workers: [] as ProtocolWorker[] }

class ProtocolWorker {
  listeners = new Map<string, Set<(event: { data: unknown }) => void>>()
  posted: Array<{ type?: string; id?: string }> = []
  terminated = false

  constructor() { protocol.workers.push(this) }

  addEventListener(type: string, listener: (event: { data: unknown }) => void) {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  postMessage(request: { type?: string; id?: string }) {
    this.posted.push(request)
    queueMicrotask(() => {
      const data = request.type === 'file'
        ? { type: 'error', id: request.id, error: 'behavioral canary response' }
        : {
            type: 'success',
            requestType: request.type,
            id: request.id,
            sentAt: Date.now(),
          }
      for (const listener of this.listeners.get('message') ?? []) listener({ data })
    })
  }

  terminate() { this.terminated = true }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.stubGlobal('Worker', ProtocolWorker)
  protocol.workers.length = 0
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('Pierre worker protocol watchdog contract', () => {
  it('clears initialization and render watchdogs from matching real-manager response IDs', async () => {
    const reportFailure = vi.fn()
    const manager = new WorkerPoolManager(
      {
        poolSize: 1,
        workerFactory: () => createMonitoredWorker(reportFailure),
      },
      { theme: PIERRE_THEMES, preferredHighlighter: PIERRE_REGEX_ENGINE },
    )
    const initialization = manager.initialize([])
    await vi.runAllTicks()
    await initialization

    const internal = manager as unknown as {
      workers: unknown[]
      executeTask: (worker: unknown, task: Record<string, unknown>) => void
    }
    const id = 'render-canary-request'
    internal.executeTask(internal.workers[0], {
      type: 'file',
      id,
      request: {
        type: 'file',
        id,
        file: {
          name: 'render-canary.ts',
          contents: 'const renderCanary = true\n',
          lang: 'typescript',
          cacheKey: 'render-canary',
        },
      },
      instances: new Set([{
        __id: 'render-canary-instance',
        onHighlightSuccess: vi.fn(),
        onHighlightError: vi.fn(),
      }]),
      primeCache: false,
      highlightKey: 'file:render-canary',
      callbacks: new Set(),
      renderOptionsVersion: 0,
      requestStart: Date.now(),
    })
    await vi.runAllTicks()

    const posted = protocol.workers.flatMap(worker => worker.posted)
    expect(posted).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: 'initialize', id: expect.any(String) }),
      expect.objectContaining({ type: 'file', id }),
    ]))

    await vi.advanceTimersByTimeAsync(PIERRE_WORKER_INITIALIZATION_TIMEOUT_MS + 250)
    expect(reportFailure).not.toHaveBeenCalled()
    expect(protocol.workers).toHaveLength(1)
    expect(protocol.workers[0].terminated).toBe(false)
    manager.terminate()
  })
})
