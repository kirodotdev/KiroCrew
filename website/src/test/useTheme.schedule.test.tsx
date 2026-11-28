/**
 * Scheduled mode (#21097): the dashboard is light from one local time and dark
 * from another, switches while it stays open, and a manual Light / Dark / Auto
 * pick still replaces the schedule. The two times are workspace-backed like the
 * mode, so another browser switches at the same times.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const themeBootFn = vi.fn()
const updateThemeConfigFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => Promise.resolve({ themes: [] }),
    themeDetail: () => Promise.resolve({}),
    themeBoot: () => themeBootFn(),
    updateThemeConfig: (body: unknown) => updateThemeConfigFn(body),
    createTheme: () => Promise.resolve({}),
    deleteTheme: () => Promise.resolve({}),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'
import { THEME_SCHEDULE_STORAGE_KEY, scheduledMode } from '../utils/themeSchedule'

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

const FIRST_RUN_DONE = { color: '', translucent_panels: '', onboarded: true, import_onboarded: true, privacy_acked: true }

async function bootWith(served: Record<string, string>) {
  themeBootFn.mockResolvedValue({ ...FIRST_RUN_DONE, mode: '', schedule_light: '', schedule_dark: '', ...served })
  const hook = renderHook(() => useTheme(), { wrapper })
  // The boot read is applied once the query settles; the first-run flag is its last write.
  await waitFor(() => expect(localStorage.getItem('mc-onboarded')).toBe('1'))
  return hook
}

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

describe('scheduledMode', () => {
  it('is light from the light time until the dark time, wrapping past midnight', () => {
    const day = { light: '07:00', dark: '19:00' }
    expect(scheduledMode(day, new Date(2026, 0, 1, 6, 59))).toBe('dark')
    expect(scheduledMode(day, new Date(2026, 0, 1, 7, 0))).toBe('light')
    expect(scheduledMode(day, new Date(2026, 0, 1, 19, 0))).toBe('dark')
    const night = { light: '19:00', dark: '07:00' }
    expect(scheduledMode(night, new Date(2026, 0, 1, 23, 30))).toBe('light')
    expect(scheduledMode(night, new Date(2026, 0, 1, 12, 0))).toBe('dark')
  })
})

describe('useTheme in Scheduled mode', () => {
  const setThemeMode = vi.fn()

  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'light')
    themeBootFn.mockReset()
    updateThemeConfigFn.mockReset().mockResolvedValue({})
    setThemeMode.mockReset()
    window.electronAPI = { setThemeMode } as unknown as typeof window.electronAPI
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })
  })
  afterEach(() => {
    vi.useRealTimers()
    delete window.electronAPI
  })

  it('turns dark within a minute of the dark time, and a manual pick replaces the schedule', async () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '07:00', dark: '19:00' }))
    const { result } = await bootWith({})
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 0, 1, 18, 59, 30))

    act(() => result.current.setTheme('schedule'))
    expect(result.current.theme).toBe('light')
    await advance(60_000)
    expect(result.current.theme).toBe('dark')
    expect(document.documentElement.dataset.mode).toBe('dark')
    // The desktop shell has no scheduled source of its own: it follows the resolved mode.
    expect(setThemeMode).toHaveBeenLastCalledWith('dark')
    expect(updateThemeConfigFn).toHaveBeenCalledWith({ mode: 'schedule' })

    act(() => result.current.setTheme('light'))
    await advance(60_000)
    expect(result.current.theme).toBe('light')
  })

  it('Toggle Theme from Scheduled picks the other appearance rather than Auto', async () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '07:00', dark: '19:00' }))
    const { result } = await bootWith({})
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 0, 1, 22, 0))
    act(() => result.current.setTheme('schedule'))
    expect(result.current.theme).toBe('dark')
    act(() => result.current.cycle())
    expect(result.current.preference).toBe('light')
    expect(result.current.theme).toBe('light')
  })

  it('saves both times to the workspace and applies them at once', async () => {
    const { result } = await bootWith({})
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 0, 1, 12, 0))
    act(() => result.current.setTheme('schedule'))
    expect(result.current.theme).toBe('light')

    act(() => result.current.setSchedule({ light: '13:00', dark: '06:00' }))
    expect(result.current.theme).toBe('dark')
    await advance(0)
    expect(updateThemeConfigFn).toHaveBeenCalledWith({ schedule_light: '13:00', schedule_dark: '06:00' })
    expect(JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')).toEqual({ light: '13:00', dark: '06:00' })
  })

  it('reports a refused schedule save, even with a mode write after it, until a later save succeeds', async () => {
    const { result } = await bootWith({})
    const settle: Array<{ resolve: (v: unknown) => void; reject: (e: Error) => void }> = []
    updateThemeConfigFn.mockImplementation((body: Record<string, unknown>) => 'schedule_light' in body
      ? new Promise((resolve, reject) => { settle.push({ resolve, reject }) })
      : Promise.resolve({}))

    act(() => result.current.setSchedule({ light: '08:00', dark: '20:00' }))
    act(() => result.current.setTheme('schedule'))
    await waitFor(() => expect(settle).toHaveLength(1))
    await act(async () => { settle[0].reject(new Error('refused')) })
    await waitFor(() => expect(result.current.scheduleSaveFailed).toBe(true))
    expect(result.current.schedule).toEqual({ light: '08:00', dark: '20:00' })

    act(() => result.current.setSchedule({ light: '08:00', dark: '20:00' }))
    expect(result.current.scheduleSaveFailed).toBe(false)
    await waitFor(() => expect(settle).toHaveLength(2))
    await act(async () => { settle[1].resolve({}) })
    expect(result.current.scheduleSaveFailed).toBe(false)
  })

  it("adopts the workspace's schedule at boot, so a second browser switches at the same times", async () => {
    vi.setSystemTime(new Date(2026, 0, 1, 22, 0))
    const { result } = await bootWith({ mode: 'schedule', schedule_light: '21:00', schedule_dark: '05:00' })
    await waitFor(() => expect(result.current.preference).toBe('schedule'))
    expect(result.current.schedule).toEqual({ light: '21:00', dark: '05:00' })
    // 22:00 is dark on the 07:00 / 19:00 default and light on the workspace's times.
    await waitFor(() => expect(result.current.theme).toBe('light'))
  })

  // Seattle on the device's UTC clock: light by the sun, dark on 07:00 / 19:00.
  const SEATTLE = { method: 'sun' as const, latitude: 47.61, longitude: -122.33 }
  const SEATTLE_EVENING = new Date('2026-06-22T02:00:00Z')

  it("keeps this browser's sunrise and sunset method across the workspace's times at boot", async () => {
    vi.setSystemTime(SEATTLE_EVENING)
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '08:00', dark: '20:00', ...SEATTLE }))
    themeBootFn.mockResolvedValue({ ...FIRST_RUN_DONE, mode: 'schedule', schedule_light: '07:00', schedule_dark: '19:00' })
    const seen: string[] = []
    const { result } = renderHook(() => {
      const theme = useTheme()
      seen.push(theme.theme)
      return theme
    }, { wrapper })
    await waitFor(() => expect(result.current.preference).toBe('schedule'))
    await waitFor(() => expect(result.current.schedule).toEqual({ light: '07:00', dark: '19:00', ...SEATTLE }))
    expect(result.current.theme).toBe('light')
    expect(seen).not.toContain('dark')
    expect(JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')).toEqual({ light: '07:00', dark: '19:00', ...SEATTLE })
  })

  it('keeps the method and location off the workspace; a time change still saves only the two times', async () => {
    const { result } = await bootWith({})
    vi.useFakeTimers()
    vi.setSystemTime(SEATTLE_EVENING)
    act(() => result.current.setTheme('schedule'))
    expect(result.current.theme).toBe('dark')
    await advance(0)
    expect(updateThemeConfigFn).toHaveBeenCalledWith({ mode: 'schedule' })
    updateThemeConfigFn.mockClear()

    act(() => result.current.setSunSchedule({ method: 'sun' }))
    act(() => result.current.setSunSchedule({ latitude: SEATTLE.latitude }))
    act(() => result.current.setSunSchedule({ longitude: SEATTLE.longitude }))
    await advance(0)
    expect(result.current.theme).toBe('light')
    expect(updateThemeConfigFn).not.toHaveBeenCalled()
    expect(JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')).toEqual({ light: '07:00', dark: '19:00', ...SEATTLE })

    act(() => result.current.setSchedule({ ...result.current.schedule, light: '06:30' }))
    await advance(0)
    expect(updateThemeConfigFn).toHaveBeenCalledTimes(1)
    expect(updateThemeConfigFn).toHaveBeenCalledWith({ schedule_light: '06:30', schedule_dark: '19:00' })
    expect(result.current.theme).toBe('light')
  })

  // Another tab of this browser saved the sun method and its location after this tab loaded.
  function writeFromAnotherTab(copy: object) {
    const newValue = JSON.stringify(copy)
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, newValue)
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: THEME_SCHEDULE_STORAGE_KEY, newValue })) })
  }
  const storedCopy = () => JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY) ?? '{}')

  it("keeps another tab's location when this tab then picks Sunrise and sunset", async () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '07:00', dark: '19:00' }))
    const { result } = await bootWith({})
    vi.useFakeTimers()
    vi.setSystemTime(SEATTLE_EVENING)
    act(() => result.current.setTheme('schedule'))
    expect(result.current.theme).toBe('dark')

    writeFromAnotherTab({ light: '07:00', dark: '19:00', ...SEATTLE })
    act(() => result.current.setSunSchedule({ method: 'sun' }))
    await advance(0)
    expect(storedCopy()).toEqual({ light: '07:00', dark: '19:00', ...SEATTLE })
    expect(result.current.theme).toBe('light')
  })

  it("keeps another tab's method and location when this tab then saves a fixed time", async () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '07:00', dark: '19:00' }))
    const { result } = await bootWith({})
    vi.useFakeTimers()
    vi.setSystemTime(SEATTLE_EVENING)
    act(() => result.current.setTheme('schedule'))
    await advance(0)
    updateThemeConfigFn.mockClear()

    writeFromAnotherTab({ light: '07:00', dark: '19:00', ...SEATTLE })
    // What Settings' time field saves: the pair built from this tab's schedule.
    act(() => result.current.setSchedule({ ...result.current.schedule, light: '06:45' }))
    await advance(0)
    expect(storedCopy()).toEqual({ light: '06:45', dark: '19:00', ...SEATTLE })
    expect(updateThemeConfigFn).toHaveBeenCalledTimes(1)
    expect(updateThemeConfigFn).toHaveBeenCalledWith({ schedule_light: '06:45', schedule_dark: '19:00' })
  })

  it("adopts another tab's copy without writing it back, so two tabs never echo each other", async () => {
    const { result } = await bootWith({})
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    try {
      // The other tab's serialisation differs from this tab's (key order, a null coordinate):
      // the one rewrite this tab makes is its own normalised copy, and it settles there.
      writeFromAnotherTab({ ...SEATTLE, longitude: null, dark: '19:00', light: '07:00' })
      await waitFor(() => expect(result.current.schedule).toEqual({ light: '07:00', dark: '19:00', method: 'sun', latitude: 47.61 }))
      const own = JSON.stringify(result.current.schedule)
      expect(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY)).toBe(own)
      setItem.mockClear()
      writeFromAnotherTab(JSON.parse(own))
      await act(async () => {})
      // The one write is the other tab's; this tab, already holding that copy, writes nothing.
      expect(setItem.mock.calls.filter(([key]) => key === THEME_SCHEDULE_STORAGE_KEY)).toHaveLength(1)
    } finally {
      setItem.mockRestore()
    }
  })
})
