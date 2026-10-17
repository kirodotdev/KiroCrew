import { describe, it, expect } from 'vitest'
import { grantEndsAt, grantTtlSeconds, MAX_TIMED_GRANT_SECONDS, secondsUntilEndOfDay } from '../utils/grantDuration'

describe('grantDuration', () => {
  it('measures an hour as 3600 seconds whatever the time of day', () => {
    expect(grantTtlSeconds('hour', new Date(2026, 0, 15, 23, 59, 0))).toBe(3600)
  })

  it('measures the rest of today up to the next local midnight', () => {
    expect(secondsUntilEndOfDay(new Date(2026, 0, 15, 18, 0, 0))).toBe(6 * 3600)
    expect(grantTtlSeconds('today', new Date(2026, 0, 15, 23, 0, 0))).toBe(3600)
  })

  it('never asks for less than one second or more than a day', () => {
    expect(secondsUntilEndOfDay(new Date(2026, 0, 15, 23, 59, 59, 900))).toBe(1)
    expect(secondsUntilEndOfDay(new Date(2026, 0, 15, 0, 0, 0))).toBe(MAX_TIMED_GRANT_SECONDS)
  })

  it('ends an hour grant an hour on and a rest-of-today grant at local midnight', () => {
    const now = new Date(2026, 0, 15, 22, 30, 0)
    expect(grantEndsAt('hour', now)).toBe(new Date(2026, 0, 15, 23, 30, 0).getTime())
    expect(grantEndsAt('today', now)).toBe(new Date(2026, 0, 16, 0, 0, 0).getTime())
  })

  it('lasts a rest-of-today grant made mid-second until midnight, not a second short', () => {
    const now = new Date(2026, 0, 15, 22, 30, 0, 400)
    expect(secondsUntilEndOfDay(now)).toBe(90 * 60)
    expect(grantEndsAt('today', now)).toBeGreaterThanOrEqual(new Date(2026, 0, 16, 0, 0, 0).getTime())
  })
})
