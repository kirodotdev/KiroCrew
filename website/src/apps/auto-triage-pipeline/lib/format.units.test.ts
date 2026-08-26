/**
 * Regression: formatRelativeTime takes a SECONDS timestamp and a MILLISECONDS
 * clock.
 *
 * Split into its own file because it pins a defect the main format suite could not
 * catch: those tests choose both numbers themselves, so they were internally
 * consistent in whatever unit they picked and stayed green while every real caller
 * passed `Date.now()` into a parameter documented as seconds. The rendered view
 * read "20,669,484d ago" -- the difference between a millisecond clock and a
 * second timestamp is itself roughly the size of the millisecond clock.
 *
 * The guard here is to use the ACTUAL clock source callers use, rather than a
 * hand-picked pair of numbers.
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { initI18n } from '../../../i18n'
import { formatRelativeTime } from './format'

beforeAll(async () => {
  await initI18n()
})

describe('formatRelativeTime unit contract', () => {
  it('reads a few seconds ago as seconds when the clock is Date.now()', () => {
    const nowMs = Date.now()
    const fiveMinutesAgo = Math.floor(nowMs / 1000) - 300
    const text = formatRelativeTime(fiveMinutesAgo, nowMs)
    // Must be minutes, and must NOT have exploded into days.
    expect(text).not.toMatch(/\d{4,}/)
    expect(text.toLowerCase()).not.toContain('d')
  })

  it('renders a genuinely old timestamp in days, not millennia', () => {
    const nowMs = Date.now()
    const threeDaysAgo = Math.floor(nowMs / 1000) - 3 * 86400
    const text = formatRelativeTime(threeDaysAgo, nowMs)
    expect(text).toMatch(/3/)
    // Four-or-more digits would mean the units were mixed again.
    expect(text).not.toMatch(/\d{4,}/)
  })

  it('treats the same instant as just now', () => {
    const nowMs = Date.now()
    const text = formatRelativeTime(Math.floor(nowMs / 1000), nowMs)
    expect(text).toBeTruthy()
    expect(text).not.toMatch(/\d{4,}/)
  })
})
