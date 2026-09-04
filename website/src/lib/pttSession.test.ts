import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createPttSession, type PttPhase, type PttSessionDeps, type VoiceControls } from './pttSession'
import { MAX_HOLD_MS } from './pushToTalk'

/**
 * The transports' behavior is pinned end-to-end by the two hook suites
 * (usePushToTalk.test.ts, useTouchPushToTalk.test.ts), which drive real DOM
 * events. What is covered HERE is the core's own contract at its seams — the
 * ownership/sequence/generation semantics both transports rely on, including
 * the two policies that differ between them (`isLatched`,
 * `disownPendingOnRelinquish`), exercised without a DOM or a gesture.
 */

/** A recording-state-tracking stand-in for useVoiceInput. */
function makeVoice(overrides: Partial<VoiceControls> = {}) {
  const calls: string[] = []
  const v: VoiceControls & { calls: string[] } = {
    calls,
    recording: false,
    start: vi.fn(() => { calls.push('start'); v.recording = true }),
    stop: vi.fn(() => { calls.push('stop'); v.recording = false }),
    cancel: vi.fn(() => { calls.push('cancel'); v.recording = false }),
    ...overrides,
  }
  return v
}

/** A `start()` whose settlement the test controls. */
function deferredStart(voice: ReturnType<typeof makeVoice>) {
  let resolve!: () => void
  let reject!: (e: unknown) => void
  const promise = new Promise<void>((res, rej) => { resolve = res; reject = rej })
  voice.start = vi.fn(() => { voice.calls.push('start'); return promise })
  return { resolve, reject }
}

/** Drain the microtasks a settled `start()` promise schedules. */
const flush = () => vi.advanceTimersByTimeAsync(0)

type Owner = 'gesture' | 'latch'

function makeHarness(overrides: Partial<PttSessionDeps<Owner>> = {}) {
  const voice = makeVoice()
  const state = { phase: 'idle' as PttPhase }
  const setPhase = vi.fn((p: PttPhase) => { state.phase = p })
  const resetToIdle = vi.fn(() => { state.phase = 'idle' })
  const disarm = vi.fn()
  const session = createPttSession<Owner>({
    voice: () => voice,
    phase: () => state.phase,
    setPhase,
    resetToIdle,
    disarm,
    ...overrides,
  })
  return { voice, state, setPhase, resetToIdle, disarm, session }
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('launch', () => {
  it('records the owner and the pending startup until the promise settles', async () => {
    const h = makeHarness()
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    expect(h.session.owner()).toBe('gesture')
    expect(h.session.startPending()).toBe(true)

    start.resolve()
    await flush()
    expect(h.session.startPending()).toBe(false)
  })

  it('treats a synchronous start as having no startup window: nothing pending, owner kept', () => {
    const h = makeHarness()
    h.voice.start = vi.fn(() => { h.voice.calls.push('start') })

    h.state.phase = 'arming'
    h.session.launch('gesture')
    expect(h.session.startPending()).toBe(false)
    expect(h.session.owner()).toBe('gesture')
  })
})

describe('settle (startup succeeded)', () => {
  it('leaves a still-owned gesture running while its phase is live', async () => {
    const h = makeHarness()
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    start.resolve()
    await flush()

    expect(h.voice.stop).not.toHaveBeenCalled()
    expect(h.session.owner()).toBe('gesture')
  })

  it('stops an ORPHAN: a gesture owner whose phase already returned to idle', async () => {
    const h = makeHarness()
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    h.state.phase = 'idle'
    start.resolve()
    await flush()

    expect(h.voice.stop).toHaveBeenCalledTimes(1)
    expect(h.session.owner()).toBeNull()
  })

  it('leaves a LATCHED owner running even at idle phase — the latch outlives its gesture', async () => {
    const h = makeHarness({ isLatched: (o) => o === 'latch' })
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    // The release adopted the session as a latch and returned the phase to idle.
    h.session.setOwner('latch')
    h.state.phase = 'idle'
    start.resolve()
    await flush()

    expect(h.voice.stop).not.toHaveBeenCalled()
    expect(h.session.owner()).toBe('latch')
  })

  it('is the backstop for a teardown that could not abort the startup: a disowned session is stopped', async () => {
    // Without disownPendingOnRelinquish (the keyboard policy), clearing the
    // owner leaves the sequence alone precisely so this handler still runs.
    const h = makeHarness()
    const start = deferredStart(h.voice)

    h.state.phase = 'holding'
    h.session.launch('gesture')
    // Teardown cleared the owner while the startup was still in flight.
    h.session.setOwner(null)
    h.state.phase = 'idle'
    start.resolve()
    await flush()

    expect(h.voice.stop).toHaveBeenCalledTimes(1)
  })

  it('ignores a stale settlement: a relaunch supersedes the sequence', async () => {
    const h = makeHarness()
    const first = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    // A second gesture opens its own startup before the first ever settles.
    const second = deferredStart(h.voice)
    h.session.launch('gesture')

    h.state.phase = 'idle'
    first.resolve()
    await flush()
    // The first settlement is "not mine any more" — it must not stop the
    // replacement session, and must not clear the pending flag the second
    // startup still owns.
    expect(h.voice.stop).not.toHaveBeenCalled()
    expect(h.session.startPending()).toBe(true)

    second.resolve()
    await flush()
    expect(h.voice.stop).toHaveBeenCalledTimes(1)
  })
})

describe('fail (startup rejected)', () => {
  it('cancels the half-acquired session, resets the transport, and clears pending', async () => {
    const h = makeHarness()
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    start.reject(new Error('mic denied'))
    await flush()

    expect(h.session.startPending()).toBe(false)
    expect(h.session.owner()).toBeNull()
    expect(h.resetToIdle).toHaveBeenCalledTimes(1)
    expect(h.voice.cancel).toHaveBeenCalledTimes(1)
  })

  it('still resets the transport when the owner was already cleared, but has nothing to cancel', async () => {
    const h = makeHarness()
    const start = deferredStart(h.voice)

    h.state.phase = 'holding'
    h.session.launch('gesture')
    h.session.setOwner(null)
    start.reject(new Error('mic denied'))
    await flush()

    expect(h.resetToIdle).toHaveBeenCalledTimes(1)
    expect(h.voice.cancel).not.toHaveBeenCalled()
  })

  it('ignores a stale rejection: a superseded startup cannot tear down its replacement', async () => {
    const h = makeHarness()
    const first = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    deferredStart(h.voice)
    h.session.launch('gesture')

    first.reject(new Error('mic denied'))
    await flush()

    expect(h.resetToIdle).not.toHaveBeenCalled()
    expect(h.voice.cancel).not.toHaveBeenCalled()
    expect(h.session.owner()).toBe('gesture')
  })
})

describe('disownPendingOnRelinquish (the touch policy)', () => {
  it('relinquishing ownership invalidates the in-flight startup: its settlement is a no-op', async () => {
    const h = makeHarness({ disownPendingOnRelinquish: true })
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    // Teardown: the gesture was abandoned mid-acquisition.
    h.session.setOwner(null)
    expect(h.session.startPending()).toBe(false)

    // A replacement session (the mic button) is now live; the abandoned
    // startup's late resolution must not stop it.
    h.state.phase = 'idle'
    start.resolve()
    await flush()
    expect(h.voice.stop).not.toHaveBeenCalled()
  })

  it('relinquishing ownership also silences the startup rejection', async () => {
    const h = makeHarness({ disownPendingOnRelinquish: true })
    const start = deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    h.session.setOwner(null)
    start.reject(new Error('mic denied'))
    await flush()

    expect(h.resetToIdle).not.toHaveBeenCalled()
    expect(h.voice.cancel).not.toHaveBeenCalled()
  })

  it('handing ownership over (non-null) does NOT disown the startup', async () => {
    const h = makeHarness({ disownPendingOnRelinquish: true })
    deferredStart(h.voice)

    h.state.phase = 'arming'
    h.session.launch('gesture')
    h.session.setOwner('gesture')
    expect(h.session.startPending()).toBe(true)
  })
})

describe('onOwnerChange', () => {
  it('observes every ownership transition, from every writer', () => {
    const seen: Array<Owner | null> = []
    const h = makeHarness({ onOwnerChange: (o) => { seen.push(o) } })
    h.voice.start = vi.fn(() => { h.voice.calls.push('start') })

    h.session.launch('gesture')
    h.session.setOwner('latch')
    h.session.setOwner(null)
    expect(seen).toEqual(['gesture', 'latch', null])
  })
})

describe('beginHold and the hard cap', () => {
  it('relabels the phase and commits the hold when the ceiling elapses', () => {
    const h = makeHarness()

    h.state.phase = 'arming'
    h.session.beginHold()
    expect(h.setPhase).toHaveBeenCalledWith('holding')

    vi.advanceTimersByTime(MAX_HOLD_MS)
    expect(h.disarm).toHaveBeenCalledWith(true)
  })

  it('does not fire against a hold that already ended: the phase moved on', () => {
    const h = makeHarness()

    h.state.phase = 'arming'
    h.session.beginHold()
    h.state.phase = 'idle'
    vi.advanceTimersByTime(MAX_HOLD_MS)
    expect(h.disarm).not.toHaveBeenCalled()
  })

  it('does not fire against a LATER hold: the generation moved on', () => {
    const h = makeHarness()

    h.state.phase = 'arming'
    h.session.beginHold()
    // A teardown bumps the generation, then a new hold begins — still 'holding'.
    h.session.bumpGeneration()
    h.state.phase = 'holding'
    vi.advanceTimersByTime(MAX_HOLD_MS)
    expect(h.disarm).not.toHaveBeenCalled()
  })

  it('clearCapTimer disarms the ceiling outright', () => {
    const h = makeHarness()

    h.state.phase = 'arming'
    h.session.beginHold()
    h.session.clearCapTimer()
    vi.advanceTimersByTime(MAX_HOLD_MS)
    expect(h.disarm).not.toHaveBeenCalled()
  })

  it('a fresh beginHold re-arms its own ceiling with its own generation', () => {
    const h = makeHarness()

    h.state.phase = 'arming'
    h.session.beginHold()
    h.session.bumpGeneration()
    h.session.clearCapTimer()

    h.session.beginHold()
    vi.advanceTimersByTime(MAX_HOLD_MS)
    expect(h.disarm).toHaveBeenCalledTimes(1)
    expect(h.disarm).toHaveBeenCalledWith(true)
  })
})
