import type { WorkerPoolManager } from '@pierre/diffs/worker'

/** `unsupported`: this environment has no `Worker` API, so Pierre renders on
 *  the main thread exactly as before this lifecycle existed — there is no pool
 *  to recover. `unavailable`: workers exist but every recovery attempt failed,
 *  so surfaces stay in app-owned plain text until reload. */
export type WorkerPoolPhase = 'unsupported' | 'unavailable' | 'starting' | 'ready' | 'recovering'

export interface WorkerPoolSnapshot {
  phase: WorkerPoolPhase
  generation: number
  pool?: WorkerPoolManager
}

export interface WorkerPoolHandle {
  pool: WorkerPoolManager
  ready: Promise<void>
  terminate: () => void
}

export interface WorkerPoolLifecycleOptions {
  create: (reportFailure: (reason?: unknown) => void) => WorkerPoolHandle
  retryDelaysMs: readonly number[]
  cooldownMs: number
  stableAfterMs: number
  warn?: (reason?: unknown) => void
}

/**
 * Owns one replaceable Pierre worker pool.
 *
 * A generation is retired as one unit: subscribers first switch to app-owned
 * plain text, then every worker and pending request in the old manager is
 * terminated. The manager's initialization promise is the readable readiness
 * boundary the old per-worker sticky latch lacked, so replacements publish
 * `ready` only after the complete generation initializes. That readable boundary
 * is why recovery owns replaceable manager generations instead of restoring the
 * simpler sticky `disableWorkerPool` latch, which cannot rebind mounted renderers.
 * Late events carry
 * their generation and cannot retire a newer pool. Repeated startup failures use short retries followed by a cooldown so
 * a broken worker bundle cannot churn indefinitely.
 */
export class WorkerPoolLifecycle {
  private readonly listeners = new Set<() => void>()
  private snapshot: WorkerPoolSnapshot
  private handle: WorkerPoolHandle | undefined
  private timer: ReturnType<typeof setTimeout> | undefined
  private stabilityTimer: ReturnType<typeof setTimeout> | undefined
  private consecutiveFailures = 0
  private warned = false
  private attemptingGeneration: number | undefined

  constructor(private readonly options: WorkerPoolLifecycleOptions) {
    this.snapshot = { phase: 'starting', generation: 0 }
  }

  getSnapshot = (): WorkerPoolSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  start(): void {
    if (this.handle || this.timer) return
    this.beginAttempt()
  }

  reportFailure(generation: number, reason?: unknown): void {
    if (generation !== this.snapshot.generation) return
    const attemptActive = this.attemptingGeneration === generation
    if (this.snapshot.phase !== 'ready' && !attemptActive) return
    this.attemptingGeneration = undefined
    if (this.stabilityTimer !== undefined) {
      clearTimeout(this.stabilityTimer)
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
      phase: exhausted ? 'unavailable' : 'recovering',
      generation,
    })

    // Publishing first makes mounted imperative Pierre instances unmount into
    // readable plain text before termination rejects their pending work. React
    // commits an external-store update in a microtask, so the terminate waits
    // for a macrotask boundary: every microtask queued by `publish` — whatever
    // order React enqueued it in — has run before the old workers are gone.
    const handle = this.handle
    this.handle = undefined
    setTimeout(() => handle?.terminate(), 0)

    // One half-open attempt follows the cooldown. If it also fails, remain in
    // app-owned plain text until reload instead of spawning workers forever.
    if (exhausted) return
    const delayMs = inCooldown
      ? this.options.cooldownMs
      : this.options.retryDelaysMs[retryIndex]
    this.timer = setTimeout(() => {
      this.timer = undefined
      if (generation === this.snapshot.generation) this.beginAttempt()
    }, delayMs)
  }

  private beginAttempt(): void {
    const generation = this.snapshot.generation + 1
    this.attemptingGeneration = generation
    this.publish({ phase: generation === 1 ? 'starting' : 'recovering', generation })

    let handle: WorkerPoolHandle
    let failedDuringCreate = false
    try {
      handle = this.options.create(reason => {
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
    if (generation === 1) this.publish({ phase: 'starting', generation, pool: handle.pool })

    void handle.ready.then(() => {
      if (generation !== this.snapshot.generation || this.handle !== handle) return
      this.attemptingGeneration = undefined
      this.publish({ phase: 'ready', generation, pool: handle.pool })
      if (this.consecutiveFailures > 0) {
        this.stabilityTimer = setTimeout(() => {
          this.stabilityTimer = undefined
          if (generation === this.snapshot.generation && this.snapshot.phase === 'ready') {
            this.consecutiveFailures = 0
            this.warned = false
          }
        }, this.options.stableAfterMs)
      }
    }).catch(error => {
      this.reportFailure(generation, error)
    })
  }

  private publish(snapshot: WorkerPoolSnapshot): void {
    this.snapshot = snapshot
    for (const listener of [...this.listeners]) listener()
  }
}
