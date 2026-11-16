import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import { i18next } from '../../i18n/all'
import { __resetSecondClockForTests } from '../../hooks/useSecondClock'
import { RotateByNote } from './RotateByNote'
import { localDay, parseLocalDay } from '../telemetryRange'

/**
 * The "Rotate by <date>" note under a vault entry. Its chip follows the viewer's
 * local calendar day while the page stays open, and the date reads the way the
 * rest of the app formats dates, in the viewer's language.
 */
describe('RotateByNote', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    __resetSecondClockForTests()
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetSecondClockForTests()
  })

  // First in the file: the shared date formatter is built on first use and keeps
  // the zone it was built in, so a later test would format in UTC whatever TZ says.
  it('reads the stored day as a local date in a zone west of UTC', () => {
    const before = process.env.TZ
    process.env.TZ = 'America/Los_Angeles'
    try {
      // A bare `new Date('2026-10-01')` is UTC midnight, which is Sept 30 here.
      expect(new Date('2026-10-01').getDate()).toBe(30)
      const d = parseLocalDay('2026-10-01')
      expect(d && [d.getFullYear(), d.getMonth(), d.getDate()]).toEqual([2026, 9, 1])
      vi.setSystemTime(new Date(2026, 9, 5, 12, 0, 0))
      render(<RotateByNote name="KEY_A" rotateBy="2026-10-01" />)
      expect(screen.getByText('Rotate by 10/1/2026')).toBeInTheDocument()
      expect(screen.getByRole('note', { name: /the rotate-by date 10\/1\/2026 has passed/ })).toBeInTheDocument()
    } finally {
      if (before === undefined) delete process.env.TZ
      else process.env.TZ = before
    }
  })

  it('shows the chip once local midnight passes on the rotate-by day, with no remount', () => {
    vi.setSystemTime(new Date(2026, 5, 15, 23, 59, 50))
    render(<RotateByNote name="KEY_A" rotateBy="2026-06-15" />)
    expect(screen.getByText(/^Rotate by /)).toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(20_000)
    })
    expect(localDay(new Date())).toBe('2026-06-16')
    expect(screen.getByRole('note')).toHaveTextContent('Rotation due')
  })

  it('keeps no chip through the whole rotate-by day', () => {
    vi.setSystemTime(new Date(2026, 5, 15, 0, 0, 5))
    render(<RotateByNote name="KEY_A" rotateBy="2026-06-15" />)
    act(() => {
      vi.advanceTimersByTime(3_600_000)
    })
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('accepts a date arriving after the first render without breaking the hook order', () => {
    vi.setSystemTime(new Date(2026, 5, 15, 12, 0, 0))
    const { rerender, container } = render(<RotateByNote name="KEY_A" />)
    expect(container).toBeEmptyDOMElement()
    rerender(<RotateByNote name="KEY_A" rotateBy="2026-06-14" />)
    expect(screen.getByRole('note')).toBeInTheDocument()
  })

  it('writes the date in the interface language', async () => {
    vi.setSystemTime(new Date(2026, 9, 5, 12, 0, 0))
    await i18next.changeLanguage('de')
    try {
      render(<RotateByNote name="KEY_A" rotateBy="2026-10-01" />)
      expect(screen.getByText('Rotieren bis 1.10.2026')).toBeInTheDocument()
      expect(screen.getByRole('note').getAttribute('aria-label')).toContain('1.10.2026')
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('falls back to the stored text for a value that is not a calendar day', () => {
    vi.setSystemTime(new Date(2026, 5, 15, 12, 0, 0))
    const { rerender } = render(<RotateByNote name="KEY_A" rotateBy="next week" />)
    expect(screen.getByText('Rotate by next week')).toBeInTheDocument()
    rerender(<RotateByNote name="KEY_A" rotateBy="2026-02-30" />)
    expect(screen.getByText('Rotate by 2026-02-30')).toBeInTheDocument()
  })
})
