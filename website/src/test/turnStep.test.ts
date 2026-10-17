import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  READING_LINE_TOLERANCE_PX,
  readingPosition,
  registerTurnStepper,
  stepTurn,
  turnStepTarget,
} from '../pages/chat/turnStep'

/**
 * The pure half of Alt+↑ / Alt+↓ turn stepping: where the reader is, which
 * turn a step lands on, and the one-stepper seam the global handler calls.
 * The keyboard path end to end is useTurnStepper.test.tsx.
 */

describe('turnStepTarget', () => {
  const stops = [0, 3, 6, 8]

  it('steps up to the previous prompt and down to the next from a turn the reader is on', () => {
    expect(turnStepTarget(stops, 3, true, -1)).toBe(0)
    expect(turnStepTarget(stops, 3, true, 1)).toBe(6)
  })

  it('has nowhere to go past either end', () => {
    expect(turnStepTarget(stops, 0, true, -1)).toBe(-1)
    expect(turnStepTarget(stops, 8, true, 1)).toBe(-1)
    expect(turnStepTarget([], 0, false, 1)).toBe(-1)
    expect(turnStepTarget([], 0, false, -1)).toBe(-1)
  })

  it('from inside a reply, steps up to that reply\'s own prompt and down to the next one', () => {
    // Row 4 is the reply to the prompt at 3; the reader is in it.
    expect(turnStepTarget(stops, 4, false, -1)).toBe(3)
    expect(turnStepTarget(stops, 4, false, 1)).toBe(6)
  })

  it('treats a prompt still below the reading line as the next stop, not the current one', () => {
    expect(turnStepTarget(stops, 6, false, 1)).toBe(6)
    expect(turnStepTarget(stops, 6, false, -1)).toBe(3)
  })

  it('steps up to the last prompt once the reader is past every row', () => {
    expect(turnStepTarget(stops, 10, false, -1)).toBe(8)
    expect(turnStepTarget(stops, 10, false, 1)).toBe(-1)
  })
})

describe('readingPosition', () => {
  const rows = (tops: number[], first = 0) => tops.map((top, i) => ({ idx: first + i, top }))

  it('returns the first row at or below the line, and whether it sits on it', () => {
    expect(readingPosition(rows([-80, 20, 120, 220]), 120)).toEqual({ at: 2, onLine: true })
    expect(readingPosition(rows([-80, 20, 200, 300]), 120)).toEqual({ at: 2, onLine: false })
  })

  it('counts a row within the tolerance of the line as on it, from either side', () => {
    const t = READING_LINE_TOLERANCE_PX
    expect(readingPosition(rows([0, 120 - t]), 120)).toEqual({ at: 1, onLine: true })
    expect(readingPosition(rows([0, 120 + t]), 120)).toEqual({ at: 1, onLine: true })
    expect(readingPosition(rows([0, 120 + t + 1]), 120)).toEqual({ at: 1, onLine: false })
    expect(readingPosition(rows([0, 120 - t - 1, 400]), 120)).toEqual({ at: 2, onLine: false })
  })

  it('places the reader after the last mounted row when every row has passed the line', () => {
    expect(readingPosition(rows([-300, -100, 50], 7), 120)).toEqual({ at: 10, onLine: false })
  })

  it('reads the first mounted row when unmounted spacer sits above it', () => {
    expect(readingPosition(rows([400, 500], 12), 120)).toEqual({ at: 12, onLine: false })
  })

  it('has no position with nothing mounted', () => {
    expect(readingPosition([], 120)).toBeNull()
  })
})

describe('the turn-stepper seam', () => {
  let unregister: (() => void) | null = null
  afterEach(() => { unregister?.(); unregister = null })

  it('reports an unclaimed step while nothing is registered', () => {
    expect(stepTurn(1)).toBe(false)
  })

  it('hands the direction to the registered stepper and returns its claim', () => {
    const stepper = vi.fn(() => true)
    unregister = registerTurnStepper(stepper)
    expect(stepTurn(-1)).toBe(true)
    expect(stepper).toHaveBeenCalledWith(-1)
  })

  it('lets a stale unregister leave a newer registration in place', () => {
    const first = vi.fn(() => true)
    const second = vi.fn(() => true)
    const dropFirst = registerTurnStepper(first)
    unregister = registerTurnStepper(second)
    dropFirst()
    stepTurn(1)
    expect(second).toHaveBeenCalledWith(1)
    expect(first).not.toHaveBeenCalled()
  })
})
