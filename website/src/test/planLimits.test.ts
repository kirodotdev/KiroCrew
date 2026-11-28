/**
 * The plan windows on `GET /api/usage/kiro` are the provider's own numbers, so
 * the parser keeps what was sent and drops what was not: a missing or malformed
 * window is null (no reading), never 0%.
 */
import { describe, expect, it } from 'vitest'
import { parsePlanLimitReading, parsePlanLimits } from '../api/planLimits'

describe('parsePlanLimitReading', () => {
  it('reads a Codex block: seconds become milliseconds and the window length is kept', () => {
    expect(parsePlanLimitReading({
      five_hour: { used_percent: 10, resets_at: 1793971700, window_minutes: 300, stale: false },
      weekly: { used_percent: 63, resets_at: 1794377116, window_minutes: 10080, stale: true },
      captured_at: '2026-11-06T10:00:00Z',
      source_mtime: 1,
    })).toEqual({
      fiveHour: { usedPercent: 10, resetsAt: 1793971700_000, windowMinutes: 300, stale: false },
      weekly: { usedPercent: 63, resetsAt: 1794377116_000, windowMinutes: 10080, stale: true },
      capturedAt: Date.parse('2026-11-06T10:00:00Z'),
    })
  })

  it('a block with no windows is still a reading, so the row can say there are no plan limits', () => {
    expect(parsePlanLimitReading({ five_hour: null, weekly: null, captured_at: '2026-11-06T10:00:00Z' }))
      .toEqual({ fiveHour: null, weekly: null, capturedAt: Date.parse('2026-11-06T10:00:00Z') })
  })

  it.each([
    ['a string percent', { used_percent: '10' }],
    ['a negative percent', { used_percent: -1 }],
    ['a non-finite percent', { used_percent: Number.NaN }],
    ['no percent', { resets_at: 1 }],
    ['an array', [10]],
  ])('drops a window with %s', (_label, window) => {
    expect(parsePlanLimitReading({ five_hour: window })?.fiveHour).toBeNull()
  })

  it('drops unusable times', () => {
    const reading = parsePlanLimitReading({
      five_hour: { used_percent: 5, resets_at: 'soon', window_minutes: 0 },
      captured_at: 'not a date',
    })
    expect(reading).toEqual({
      fiveHour: { usedPercent: 5, resetsAt: null, windowMinutes: null, stale: false },
      weekly: null,
      capturedAt: null,
    })
  })

  it.each([null, undefined, 'x', 3, []])('no object is no reading: %s', raw => {
    expect(parsePlanLimitReading(raw)).toBeNull()
  })
})

describe('parsePlanLimits', () => {
  it("reads Codex's reading, and an older gateway without the key as no reading", () => {
    expect(parsePlanLimits({})).toEqual({ codex: null })
    expect(parsePlanLimits(undefined)).toEqual({ codex: null })
    expect(parsePlanLimits({ codex: { five_hour: { used_percent: 1 } } }).codex?.fiveHour?.usedPercent).toBe(1)
  })
})
