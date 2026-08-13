/**
 * Completion-gate decision logic, tested at the pure-function layer (like the walk
 * geometry) so the slot bookkeeping and history fetch around it need not be
 * simulated.
 *
 * Each group below guards a specific class of bug that once reached a real user:
 *
 *  - "own-background-agent" — the companion's own housekeeping agent celebrating
 *    its own timers. Its completions must never notify.
 *  - "never-running" / "opted-out" — announcing work that was never observed, or
 *    that the user explicitly silenced.
 *  - "threshold boundary" — the original 30s cutoff swallowed ~half of all real
 *    turns; these pin the 10s boundary exactly (at, ±1ms) so it cannot drift back.
 *  - "assumed start defers to verify" — an app restart mid-turn reset elapsed to
 *    ~zero and dropped the ping; an assumed start must be sent to `verify`, never
 *    dropped as too-short.
 *  - "confirmAssumedStart" — the recovery half of the verify path, including its
 *    own too-short-after-recovery boundary and the history-unavailable fallback.
 */
import { describe, it, expect } from 'vitest'
import {
  TURN_NOTIFY_MIN_MS,
  BG_AGENT_PREFIX,
  isOwnBackgroundAgent,
  evaluateCompletion,
  confirmAssumedStart,
  type GateInput,
} from '../apps/crew-companion/completionGate'

/** A running, non-excluded, non-silent slot with a known start. Override per case. */
function input(over: Partial<GateInput> = {}): GateInput {
  return {
    slotKey: 'session-1',
    startedAt: 0,
    now: TURN_NOTIFY_MIN_MS,
    assumedStart: false,
    silent: false,
    ...over,
  }
}

describe('isOwnBackgroundAgent', () => {
  it('matches the exact prefix and any slot that starts with it', () => {
    expect(isOwnBackgroundAgent(BG_AGENT_PREFIX)).toBe(true)
    expect(isOwnBackgroundAgent(`${BG_AGENT_PREFIX}-1`)).toBe(true)
  })

  it('does not match ordinary session slots', () => {
    expect(isOwnBackgroundAgent('session-1')).toBe(false)
    expect(isOwnBackgroundAgent('kirocrew')).toBe(false)
    // A slot that merely CONTAINS the prefix mid-string is not the bg agent.
    expect(isOwnBackgroundAgent(`x-${BG_AGENT_PREFIX}`)).toBe(false)
  })
})

describe('evaluateCompletion — skips that must win before any timing', () => {
  it('skips the companion\'s own background agent (never celebrate own timers)', () => {
    // Long elapsed AND running — still excluded, because the agent identity wins.
    const d = evaluateCompletion(input({ slotKey: BG_AGENT_PREFIX, now: 10 * TURN_NOTIFY_MIN_MS }))
    expect(d).toEqual({ action: 'skip', reason: 'own-background-agent' })
  })

  it('skips a slot we never saw running (no start recorded)', () => {
    const d = evaluateCompletion(input({ startedAt: undefined }))
    expect(d).toEqual({ action: 'skip', reason: 'never-running' })
  })

  it('skips when the user opted out of completion pings', () => {
    // Passes the threshold, but silent wins.
    const d = evaluateCompletion(input({ silent: true, now: 10 * TURN_NOTIFY_MIN_MS }))
    expect(d).toEqual({ action: 'skip', reason: 'user-opted-out' })
  })

  it('own-background-agent beats never-running when both apply', () => {
    const d = evaluateCompletion(input({ slotKey: BG_AGENT_PREFIX, startedAt: undefined }))
    expect(d).toEqual({ action: 'skip', reason: 'own-background-agent' })
  })
})

describe('evaluateCompletion — the too-short threshold boundary', () => {
  it('skips one ms below the threshold', () => {
    const d = evaluateCompletion(input({ startedAt: 0, now: TURN_NOTIFY_MIN_MS - 1 }))
    expect(d).toEqual({ action: 'skip', reason: 'too-short' })
  })

  it('notifies exactly at the threshold (>= is the pass condition)', () => {
    const d = evaluateCompletion(input({ startedAt: 0, now: TURN_NOTIFY_MIN_MS }))
    expect(d).toEqual({ action: 'notify', elapsedMs: TURN_NOTIFY_MIN_MS })
  })

  it('notifies one ms above the threshold', () => {
    const d = evaluateCompletion(input({ startedAt: 0, now: TURN_NOTIFY_MIN_MS + 1 }))
    expect(d).toEqual({ action: 'notify', elapsedMs: TURN_NOTIFY_MIN_MS + 1 })
  })
})

describe('evaluateCompletion — assumed start defers to verify', () => {
  it('returns verify (not skip) even when the assumed elapsed is far too short', () => {
    // This is the restart-mid-turn case: elapsed looks tiny because of OUR uptime,
    // so it must NOT be dropped as too-short — it goes to history recovery.
    const d = evaluateCompletion(input({ assumedStart: true, startedAt: 0, now: 5 }))
    expect(d).toEqual({ action: 'verify', elapsedMs: 5 })
  })

  it('returns verify even when the assumed elapsed already passes the threshold', () => {
    const d = evaluateCompletion(input({ assumedStart: true, startedAt: 0, now: 10 * TURN_NOTIFY_MIN_MS }))
    expect(d).toEqual({ action: 'verify', elapsedMs: 10 * TURN_NOTIFY_MIN_MS })
  })
})

describe('confirmAssumedStart — recovery half of the verify path', () => {
  // A positive recovered start (realStart > 0) exercises the real threshold, not
  // the history-unavailable fallback below.
  const RECOVERED_START = 1_000

  it('skips one ms below the threshold after recovery', () => {
    const d = confirmAssumedStart(RECOVERED_START, RECOVERED_START + TURN_NOTIFY_MIN_MS - 1, 0)
    expect(d).toEqual({ action: 'skip', reason: 'too-short-after-recovery' })
  })

  it('notifies exactly at the threshold after recovery', () => {
    const d = confirmAssumedStart(RECOVERED_START, RECOVERED_START + TURN_NOTIFY_MIN_MS, 0)
    expect(d).toEqual({ action: 'notify', elapsedMs: TURN_NOTIFY_MIN_MS })
  })

  it('notifies one ms above the threshold after recovery', () => {
    const d = confirmAssumedStart(RECOVERED_START, RECOVERED_START + TURN_NOTIFY_MIN_MS + 1, 0)
    expect(d).toEqual({ action: 'notify', elapsedMs: TURN_NOTIFY_MIN_MS + 1 })
  })

  it('falls back to the assumed elapsed when history was unavailable (realStart <= 0)', () => {
    // realStart of 0 means "couldn't recover" — use the fallback rather than
    // computing a bogus elapsed from epoch 0. Here the fallback passes.
    const d = confirmAssumedStart(0, 999, TURN_NOTIFY_MIN_MS)
    expect(d).toEqual({ action: 'notify', elapsedMs: TURN_NOTIFY_MIN_MS })
  })

  it('falls back to the assumed elapsed when unavailable, and can still skip if that is too short', () => {
    const d = confirmAssumedStart(0, 999, TURN_NOTIFY_MIN_MS - 1)
    expect(d).toEqual({ action: 'skip', reason: 'too-short-after-recovery' })
  })
})
