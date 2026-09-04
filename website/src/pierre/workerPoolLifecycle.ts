import { useSyncExternalStore } from 'react'

export type WorkerPoolPhase = 'unavailable' | 'starting' | 'ready' | 'recovering' | 'cooldown'

export interface WorkerPoolSnapshot<Pool> {
  phase: WorkerPoolPhase
  generation: number
  pool?: Pool
}

export interface WorkerPoolHandle<Pool> {
  pool: Pool
  ready: Promise<void>
  terminate: () => void
}

export interface WorkerPoolLifecycleOptions<Pool> {
  enabled: boolean
  create: (generation: number, reportFailure: (reason?: unknown) => void) => WorkerPoolHandle<Pool>
  retryDelaysMs: readonly number[]
  cooldownMs: number
  stableAfterMs: number
  schedule?: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>
  cancel?: (timer: ReturnType<typeof setTimeout>) => void
  warn?: (reason?: unknown) => void
}

/**
 * Owns one replaceable Pierre worker pool.
 *
 * A generation is retired as one unit: subscribers first switch to app-owned
 * plain text, then every worker and pending request in the old manager is
 * terminated. Late events carry their generation and cannot retire a newer
 * pool. Repeated startup failures use short retries followed by a cooldown so
 * a broken worker bundle cannot churn indefinitely.
 */
export class WorkerPoolLifecycle<Pool> {
  private readonly listeners = new Set<() => void>()
  private readonly schedule: NonNullable<WorkerPoolLifecycleOptions<Pool>['schedule']>
  private readonly cancel: NonNullable<WorkerPoolLifecycleOptions<Pool>['cancel']>
  private snapshot: WorkerPoolSnapshot<Pool>
  private handle: WorkerPoolHandle<Pool> | undefined
  private timer: ReturnType<typeof setTimeout> | undefined
  private stabilityTimer: ReturnType<typeof setTimeout> | undefined
  private consecutiveFailures = 0
  private warned = false
  private attemptingGeneration: number | undefined
  private stopped = false

  constructor(private readonly options: WorkerPoolLifecycleOptions<Pool>) {
    this.schedule = options.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs))
    this.cancel = options.cancel ?? (timer => clearTimeout(timer))
    this.snapshot = {
      phase: options.enabled ? 'starting' : 'unavailable',
      generation: 0,
    }
  }

  getSnapshot = (): WorkerPoolSnapshot<Pool> => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  start(): void {
    if (!this.options.enabled || this.stopped || this.handle || this.timer) return
    this.beginAttempt()
  }

  stop(): void {
    this.stopped = true
    this.attemptingGeneration = undefined
    if (this.timer !== undefined) {
      this.cancel(this.timer)
      this.timer = undefined
    }
    if (this.stabilityTimer !== undefined) {
      this.cancel(this.stabilityTimer)
      this.stabilityTimer = undefined
    }
    const handle = this.handle
    this.handle = undefined
    handle?.terminate()
    this.publish({ phase: 'unavailable', generation: this.snapshot.generation + 1 })
  }

  reportFailure(generation: number, reason?: unknown): void {
    if (this.stopped || generation !== this.snapshot.generation) return
    const attemptActive = this.attemptingGeneration === generation
    if (this.snapshot.phase !== 'ready' && !attemptActive) return
    this.attemptingGeneration = undefined
    if (this.stabilityTimer !== undefined) {
      this.cancel(this.stabilityTimer)
      this.stabilityTimer = undefined
    }

    this.consecutiveFailures += 1
    const retryIndex = this.consecutiveFailures - 1
    const inCooldown = retryIndex === this.options.retryDelaysMs.length
    const exhausted = retryIndex > this.options.retryDelaysMs.length

    if (!this.warned) {
      this.warned = true
      this.options.warn?.(reason)
    }
    this.publish({
      phase: exhausted ? 'unavailable' : inCooldown ? 'cooldown' : 'recovering',
      generation,
    })

    // Publishing first makes mounted imperative Pierre instances unmount into
    // readable plain text before termination rejects their pending work.
    const handle = this.handle
    this.handle = undefined
    queueMicrotask(() => handle?.terminate())

    // One half-open attempt follows the cooldown. If it also fails, remain in
    // app-owned plain text until reload instead of spawning workers forever.
    if (exhausted) return
    const delayMs = inCooldown
      ? this.options.cooldownMs
      : this.options.retryDelaysMs[retryIndex]
    this.timer = this.schedule(() => {
      this.timer = undefined
      if (!this.stopped && generation === this.snapshot.generation) this.beginAttempt()
    }, delayMs)
  }

  private beginAttempt(): void {
    const generation = this.snapshot.generation + 1
    this.attemptingGeneration = generation
    this.publish({ phase: generation === 1 ? 'starting' : 'recovering', generation })

    let handle: WorkerPoolHandle<Pool>
    let failedDuringCreate = false
    try {
      handle = this.options.create(generation, reason => {
        failedDuringCreate = true
        this.reportFailure(generation, reason)
      })
    } catch (error) {
      this.reportFailure(generation, error)
      return
    }
    if (failedDuringCreate || generation !== this.snapshot.generation) {
      handle.terminate()
      return
    }
    this.handle = handle

    void handle.ready.then(() => {
      if (this.stopped || generation !== this.snapshot.generation || this.handle !== handle) return
      this.attemptingGeneration = undefined
      this.publish({ phase: 'ready', generation, pool: handle.pool })
      if (this.consecutiveFailures > 0) {
        this.stabilityTimer = this.schedule(() => {
          this.stabilityTimer = undefined
          if (!this.stopped && generation === this.snapshot.generation && this.snapshot.phase === 'ready') {
            this.consecutiveFailures = 0
            this.warned = false
          }
        }, this.options.stableAfterMs)
      }
    }).catch(error => {
      this.reportFailure(generation, error)
    })
  }

  private publish(snapshot: WorkerPoolSnapshot<Pool>): void {
    this.snapshot = snapshot
    for (const listener of [...this.listeners]) listener()
  }
}

export function useWorkerPoolLifecycle<Pool>(lifecycle: WorkerPoolLifecycle<Pool>): WorkerPoolSnapshot<Pool> {
  return useSyncExternalStore(lifecycle.subscribe, lifecycle.getSnapshot, lifecycle.getSnapshot)
}
