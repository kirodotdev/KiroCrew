import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { THEME_SCHEDULE_STORAGE_KEY, DEFAULT_THEME_SCHEDULE, type ThemeSchedule } from '../utils/themeSchedule'
import { writeThemeSchedule, readThemeSchedule } from '../hooks/useThemeSchedule'

const theme = vi.hoisted(() => ({ preference: 'system' as string, setTheme: vi.fn() }))
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({ preference: theme.preference, setTheme: theme.setTheme }),
}))

import ThemeScheduleDriver from './ThemeScheduleDriver'

function setSchedule(patch: Partial<ThemeSchedule>) {
  writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE, ...patch })
}

describe('ThemeScheduleDriver', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // vitest pins TZ=UTC, so these instants are also the device clock times.
    vi.setSystemTime(new Date('2026-03-10T18:00:00Z'))
    theme.preference = 'system'
    theme.setTheme.mockReset()
    theme.setTheme.mockImplementation((mode: string) => { theme.preference = mode })
    localStorage.removeItem(THEME_SCHEDULE_STORAGE_KEY)
  })
  afterEach(() => {
    vi.useRealTimers()
    localStorage.removeItem(THEME_SCHEDULE_STORAGE_KEY)
  })

  it('does nothing while the schedule is off', () => {
    setSchedule({ enabled: false, method: 'fixed', lightAt: '07:00', darkAt: '19:00' })
    render(<ThemeScheduleDriver />)
    act(() => { vi.advanceTimersByTime(2 * 60 * 60 * 1000) })
    expect(theme.setTheme).not.toHaveBeenCalled()
  })

  it('applies the scheduled mode at once, then switches at the next boundary', () => {
    setSchedule({ enabled: true, method: 'fixed', lightAt: '07:00', darkAt: '19:00' })
    const { rerender } = render(<ThemeScheduleDriver />)
    expect(theme.setTheme).toHaveBeenLastCalledWith('light')
    expect(readThemeSchedule().appliedAt).toBe(Date.parse('2026-03-10T07:00:00Z'))

    act(() => { vi.advanceTimersByTime(61 * 60 * 1000) })
    rerender(<ThemeScheduleDriver />)
    expect(theme.setTheme).toHaveBeenLastCalledWith('dark')
    expect(theme.setTheme).toHaveBeenCalledTimes(2)
  })

  it('keeps a mode the user picks by hand until the next boundary', () => {
    setSchedule({ enabled: true, method: 'fixed', lightAt: '07:00', darkAt: '19:00' })
    const { rerender } = render(<ThemeScheduleDriver />)
    expect(theme.setTheme).toHaveBeenCalledTimes(1)

    // Manual change: the Mode picker sets dark during the light period.
    theme.preference = 'dark'
    rerender(<ThemeScheduleDriver />)
    act(() => { vi.advanceTimersByTime(30 * 60 * 1000) })
    expect(theme.setTheme).toHaveBeenCalledTimes(1)
    expect(theme.preference).toBe('dark')

    // Then the user picks light by hand after sunset; the next switch is 07:00.
    act(() => { vi.advanceTimersByTime(60 * 60 * 1000) })
    theme.preference = 'light'
    rerender(<ThemeScheduleDriver />)
    act(() => { vi.advanceTimersByTime(60 * 60 * 1000) })
    expect(theme.preference).toBe('light')
  })
})
