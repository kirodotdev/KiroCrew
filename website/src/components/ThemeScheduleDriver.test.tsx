import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { THEME_SCHEDULE_STORAGE_KEY, DEFAULT_THEME_SCHEDULE, type ThemeSchedule } from '../utils/themeSchedule'
import { writeThemeSchedule, readThemeSchedule } from '../hooks/useThemeSchedule'

const theme = vi.hoisted(() => ({
  preference: 'system' as string,
  setTheme: vi.fn(),
  applyLocalTheme: vi.fn(),
  bootReady: true,
}))
vi.mock('../hooks/useTheme', () => ({
  useTheme: () => ({
    preference: theme.preference,
    setTheme: theme.setTheme,
    applyLocalTheme: theme.applyLocalTheme,
    themeBootReady: theme.bootReady,
  }),
}))

import ThemeScheduleDriver from './ThemeScheduleDriver'

function setSchedule(patch: Partial<ThemeSchedule>) {
  writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE, ...patch })
}

const FIXED = { enabled: true, method: 'fixed', lightAt: '07:00', darkAt: '19:00' } as const

describe('ThemeScheduleDriver', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // vitest pins TZ=UTC, so these instants are also the device clock times.
    vi.setSystemTime(new Date('2026-03-10T18:00:00Z'))
    theme.preference = 'system'
    theme.bootReady = true
    theme.setTheme.mockReset()
    theme.applyLocalTheme.mockReset()
    theme.applyLocalTheme.mockImplementation((mode: string) => { theme.preference = mode })
    localStorage.removeItem(THEME_SCHEDULE_STORAGE_KEY)
  })
  afterEach(() => {
    vi.useRealTimers()
    localStorage.removeItem(THEME_SCHEDULE_STORAGE_KEY)
  })

  it('does nothing while the schedule is off', () => {
    setSchedule({ ...FIXED, enabled: false })
    render(<ThemeScheduleDriver />)
    act(() => { vi.advanceTimersByTime(2 * 60 * 60 * 1000) })
    expect(theme.applyLocalTheme).not.toHaveBeenCalled()
  })

  it('applies the scheduled mode at once, then switches at the next boundary', () => {
    setSchedule(FIXED)
    const { rerender } = render(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('light')

    act(() => { vi.advanceTimersByTime(61 * 60 * 1000) })
    rerender(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('dark')
    expect(theme.applyLocalTheme).toHaveBeenCalledTimes(2)
  })

  it('never saves a scheduled switch as the workspace mode', () => {
    // setTheme writes PUT /api/config/theme, which every other device boots into.
    setSchedule(FIXED)
    const { rerender } = render(<ThemeScheduleDriver />)
    act(() => { vi.advanceTimersByTime(61 * 60 * 1000) })
    rerender(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenCalledTimes(2)
    expect(theme.setTheme).not.toHaveBeenCalled()
  })

  it('keeps a mode the user picks by hand until the next boundary', () => {
    setSchedule(FIXED)
    const { rerender } = render(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenCalledTimes(1)

    // Manual change: the Mode picker sets dark during the light period.
    theme.preference = 'dark'
    rerender(<ThemeScheduleDriver />)
    expect(readThemeSchedule().pickedAt).toBe(Date.parse('2026-03-10T18:00:00Z'))
    act(() => { vi.advanceTimersByTime(30 * 60 * 1000) })
    expect(theme.applyLocalTheme).toHaveBeenCalledTimes(1)
    expect(theme.preference).toBe('dark')

    // Then the user picks light by hand after sunset; the next switch is 07:00.
    act(() => { vi.advanceTimersByTime(60 * 60 * 1000) })
    theme.preference = 'light'
    rerender(<ThemeScheduleDriver />)
    act(() => { vi.advanceTimersByTime(60 * 60 * 1000) })
    expect(theme.preference).toBe('light')
  })

  it('waits for the theme boot read before applying', () => {
    theme.bootReady = false
    setSchedule(FIXED)
    const { rerender } = render(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).not.toHaveBeenCalled()

    theme.bootReady = true
    rerender(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('light')
  })

  it('puts the scheduled mode back after a reload adopts the workspace mode', () => {
    // This device showed dark at 19:00; on reload the boot read adopts the
    // workspace's light, which the schedule did not write.
    vi.setSystemTime(new Date('2026-03-10T20:00:00Z'))
    setSchedule(FIXED)
    theme.bootReady = false
    theme.preference = 'light'
    const { rerender } = render(<ThemeScheduleDriver />)
    theme.bootReady = true
    rerender(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('dark')
    expect(readThemeSchedule().pickedAt).toBeNull()
  })

  it('follows a boundary another tab reached while this tab was booting', () => {
    setSchedule(FIXED)
    theme.bootReady = false
    theme.preference = 'light'
    const { rerender } = render(<ThemeScheduleDriver />)
    // Sunset passes and another tab switches before this tab's boot read lands.
    vi.setSystemTime(new Date('2026-03-10T19:00:05Z'))
    theme.bootReady = true
    rerender(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('dark')
  })

  it('switches at a boundary even when another tab reached it first', () => {
    setSchedule(FIXED)
    render(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('light')

    vi.setSystemTime(new Date('2026-03-10T19:00:05Z'))
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(theme.applyLocalTheme).toHaveBeenLastCalledWith('dark')
  })

  it('holds a pick made in another tab of this device', () => {
    setSchedule(FIXED)
    render(<ThemeScheduleDriver />)
    expect(theme.applyLocalTheme).toHaveBeenCalledTimes(1)

    const picked = JSON.stringify({ ...readThemeSchedule(), pickedAt: Date.parse('2026-03-10T18:10:00Z') })
    vi.setSystemTime(new Date('2026-03-10T18:10:00Z'))
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, picked)
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: THEME_SCHEDULE_STORAGE_KEY, newValue: picked })) })
    act(() => { vi.advanceTimersByTime(30 * 60 * 1000) })
    expect(theme.applyLocalTheme).toHaveBeenCalledTimes(1)
  })

  it('still holds a manual pick when storage refuses to record it', () => {
    setSchedule(FIXED)
    const original = Storage.prototype.setItem
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === THEME_SCHEDULE_STORAGE_KEY) throw new DOMException('blocked', 'SecurityError')
      return original.call(this, key, value)
    })
    try {
      const { rerender } = render(<ThemeScheduleDriver />)
      expect(theme.applyLocalTheme).toHaveBeenCalledTimes(1)
      theme.preference = 'dark'
      rerender(<ThemeScheduleDriver />)
      act(() => { vi.advanceTimersByTime(30 * 60 * 1000) })
      expect(theme.applyLocalTheme).toHaveBeenCalledTimes(1)
      expect(theme.preference).toBe('dark')
    } finally {
      spy.mockRestore()
    }
  })
})
