import { describe, it, expect, beforeEach } from 'vitest'
import { _resetTurnClockForTest, noteTurnActivity, takeTurnSeconds } from '../hooks/turnClock'

describe('turnClock', () => {
  beforeEach(() => { _resetTurnClockForTest() })

  it('reports whole seconds since the first row and clears the stamp', () => {
    noteTurnActivity('s', 1_000, 1_000)
    noteTurnActivity('s', 9_000, 9_000)
    expect(takeTurnSeconds('s', 12_999, 12_999)).toBe(11)
    expect(takeTurnSeconds('s', 20_000, 20_000)).toBeNull()
  })

  it('answers null for a turn it never saw begin', () => {
    expect(takeTurnSeconds('never', 5_000, 5_000)).toBeNull()
  })

  it('never reports a negative length when both clocks step back', () => {
    noteTurnActivity('s', 10_000, 10_000)
    expect(takeTurnSeconds('s', 4_000, 4_000)).toBe(0)
  })

  it('reads the monotonic clock when the wall clock steps back', () => {
    noteTurnActivity('s', 500_000, 1_000)
    expect(takeTurnSeconds('s', 470_000, 91_000)).toBe(90)
  })

  it('reads the wall clock when the monotonic one stood still, as across a sleep', () => {
    noteTurnActivity('s', 0, 1_000)
    expect(takeTurnSeconds('s', 600_000, 3_000)).toBe(600)
  })

  it('keeps sessions apart', () => {
    noteTurnActivity('a', 0, 0)
    noteTurnActivity('b', 5_000, 5_000)
    expect(takeTurnSeconds('a', 10_000, 10_000)).toBe(10)
    expect(takeTurnSeconds('b', 10_000, 10_000)).toBe(5)
  })
})
