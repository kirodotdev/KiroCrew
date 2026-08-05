import { describe, it, expect, vi } from 'vitest'
import { decideStopAction, handleStopPress, isEscalationState, FORCE_KILL_ARMING_MS } from '../utils/stopDebounce'

describe('decideStopAction', () => {
  it('first press (not soft_pending) is a soft cancel', () => {
    // softStopAt is irrelevant when not yet pending
    expect(decideStopAction(false, 1_000, 0)).toBe('soft')
    expect(decideStopAction(false, 5_000, 4_999)).toBe('soft')
  })

  it('ignores a second press inside the arming window (rapid double-tap)', () => {
    const softStopAt = 10_000
    expect(decideStopAction(true, softStopAt + 1, softStopAt)).toBe('ignore')
    expect(decideStopAction(true, softStopAt + 200, softStopAt)).toBe('ignore')
    // Same-instant double event
    expect(decideStopAction(true, softStopAt, softStopAt)).toBe('ignore')
  })

  it('escalates to force exactly at the arming boundary', () => {
    const softStopAt = 10_000
    // < armingMs => ignore; >= armingMs => force
    expect(decideStopAction(true, softStopAt + FORCE_KILL_ARMING_MS - 1, softStopAt)).toBe('ignore')
    expect(decideStopAction(true, softStopAt + FORCE_KILL_ARMING_MS, softStopAt)).toBe('force')
  })

  it('forces a deliberate second press after the arming window', () => {
    const softStopAt = 10_000
    expect(decideStopAction(true, softStopAt + 1_000, softStopAt)).toBe('force')
    expect(decideStopAction(true, softStopAt + 10_000, softStopAt)).toBe('force')
  })

  it('a never-armed first force press (softStopAt=0) is allowed to force', () => {
    // Defensive: if state is somehow soft_pending with no recorded soft press,
    // a press far in the future should still be able to force.
    expect(decideStopAction(true, Date.now(), 0)).toBe('force')
  })

  it('respects a custom arming window', () => {
    const softStopAt = 1_000
    expect(decideStopAction(true, softStopAt + 50, softStopAt, 100)).toBe('ignore')
    expect(decideStopAction(true, softStopAt + 100, softStopAt, 100)).toBe('force')
  })

  it('exports a sane default arming window', () => {
    expect(FORCE_KILL_ARMING_MS).toBeGreaterThan(0)
    expect(FORCE_KILL_ARMING_MS).toBeLessThanOrEqual(1000)
  })
})

describe('handleStopPress (ChatPage onStop wiring)', () => {
  it('first press records the timestamp and fires the soft cancel only', () => {
    const ref = { current: 0 }
    const onSoft = vi.fn()
    const onForce = vi.fn()
    const action = handleStopPress(false, 1_000, ref, onSoft, onForce)
    expect(action).toBe('soft')
    expect(ref.current).toBe(1_000) // ref set on soft
    expect(onSoft).toHaveBeenCalledTimes(1)
    expect(onForce).not.toHaveBeenCalled()
  })

  it('swallows a rapid second press inside the window (no dispatch, ref unchanged)', () => {
    const ref = { current: 10_000 }
    const onSoft = vi.fn()
    const onForce = vi.fn()
    const action = handleStopPress(true, 10_200, ref, onSoft, onForce) // +200ms < 400ms
    expect(action).toBe('ignore')
    expect(onSoft).not.toHaveBeenCalled()
    expect(onForce).not.toHaveBeenCalled()
    expect(ref.current).toBe(10_000) // untouched
  })

  it('fires the force kill (force=true path) for a press after the window', () => {
    const ref = { current: 10_000 }
    const onSoft = vi.fn()
    const onForce = vi.fn()
    const action = handleStopPress(true, 10_000 + FORCE_KILL_ARMING_MS, ref, onSoft, onForce)
    expect(action).toBe('force')
    expect(onForce).toHaveBeenCalledTimes(1)
    expect(onSoft).not.toHaveBeenCalled()
  })

  it('models the real sequence: soft press then deliberate force after the window', () => {
    const ref = { current: 0 }
    const onSoft = vi.fn()
    const onForce = vi.fn()
    // t=1000 first press -> soft, ref=1000
    expect(handleStopPress(false, 1_000, ref, onSoft, onForce)).toBe('soft')
    // t=1100 accidental double-tap (now soft_pending) -> ignored
    expect(handleStopPress(true, 1_100, ref, onSoft, onForce)).toBe('ignore')
    // t=1600 deliberate force (>400ms later) -> force
    expect(handleStopPress(true, 1_600, ref, onSoft, onForce)).toBe('force')
    expect(onSoft).toHaveBeenCalledTimes(1)
    expect(onForce).toHaveBeenCalledTimes(1)
  })

  it('per-slot refs: one slot soft press does not arm another slot (no cross-slot ignore)', () => {
    // Mirrors ChatPage's per-slot Map<slotId, number> view. Slot A's recent
    // soft press must NOT cause slot B's force press to be swallowed.
    const map = new Map<string, number>()
    const viewFor = (slot: string) => ({
      get current() { return map.get(slot) ?? 0 },
      set current(v: number) { map.set(slot, v) },
    })
    const noop = () => {}
    // t=1000: soft press on slot A -> records A only
    expect(handleStopPress(false, 1_000, viewFor('A'), noop, noop)).toBe('soft')
    expect(map.get('A')).toBe(1_000)
    expect(map.has('B')).toBe(false)
    // t=1100: press on already-soft_pending slot B, only 100ms after A's soft
    // press. With a shared ref this would wrongly 'ignore'; per-slot it forces.
    expect(handleStopPress(true, 1_100, viewFor('B'), noop, noop)).toBe('force')
  })
})


describe('isEscalationState', () => {
  it('returns false for idle/undefined states (press = soft cancel)', () => {
    expect(isEscalationState(undefined)).toBe(false)
    expect(isEscalationState(null)).toBe(false)
    expect(isEscalationState('idle')).toBe(false)
  })

  it('returns true for soft_pending (second press = force kill)', () => {
    expect(isEscalationState('soft_pending')).toBe(true)
  })

  it('returns true for killing (escape hatch must re-dispatch force)', () => {
    expect(isEscalationState('killing')).toBe(true)
  })

  it('killing-state escape hatch press dispatches force, not soft', () => {
    // The escape hatch appears after 15s of killing; the original soft press
    // is far outside the 400ms arming window, so the press must escalate.
    const onSoft = vi.fn()
    const onForce = vi.fn()
    const ref = { current: Date.now() - 16_000 }
    const action = handleStopPress(
      isEscalationState('killing'), Date.now(), ref, onSoft, onForce,
    )
    expect(action).toBe('force')
    expect(onForce).toHaveBeenCalledTimes(1)
    expect(onSoft).not.toHaveBeenCalled()
  })
})
