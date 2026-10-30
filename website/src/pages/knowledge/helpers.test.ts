import { describe, it, expect, afterEach, vi } from 'vitest'
import { formatRelativeDate } from './helpers'

// formatRelativeDate renders the "last synced" label on a knowledge source row
// (SourcesList.tsx -> StalenessIndicator). "today" and "yesterday" are
// calendar-day words to a user, so the buckets must count local calendar days,
// not elapsed 24-hour periods. Every date is built with the local Date
// constructor, so these hold in any host timezone.
describe('formatRelativeDate', () => {
  afterEach(() => { vi.useRealTimers() })

  const viewAt = (now: Date) => {
    vi.useFakeTimers()
    vi.setSystemTime(now)
  }

  it('labels a sync from yesterday evening as "yesterday" when viewed after midnight', () => {
    // 90 minutes elapsed, but a different calendar day.
    viewAt(new Date(2026, 9, 26, 0, 30, 0))
    expect(formatRelativeDate(new Date(2026, 9, 25, 23, 0, 0).toISOString())).toBe('yesterday')
  })

  it('labels a sync from two evenings ago as 2 days ago, not "yesterday", when viewed in the morning', () => {
    // 34 hours elapsed, but two calendar days back.
    viewAt(new Date(2026, 9, 26, 9, 0, 0))
    expect(formatRelativeDate(new Date(2026, 9, 24, 23, 0, 0).toISOString())).toBe('2d ago')
  })

  it('labels a sync from earlier the same day as "today"', () => {
    viewAt(new Date(2026, 9, 26, 23, 30, 0))
    expect(formatRelativeDate(new Date(2026, 9, 26, 0, 15, 0).toISOString())).toBe('today')
  })

  it('labels a future timestamp (clock skew) as "today"', () => {
    viewAt(new Date(2026, 9, 26, 12, 0, 0))
    expect(formatRelativeDate(new Date(2026, 9, 27, 1, 0, 0).toISOString())).toBe('today')
  })
})
