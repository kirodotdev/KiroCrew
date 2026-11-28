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
const themeConfigFn = vi.fn()
const updateThemeConfigFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => Promise.resolve({ themes: [] }),
    themeDetail: () => Promise.resolve({}),
    themeBoot: () => themeBootFn(),
    themeConfig: () => themeConfigFn(),
    updateThemeConfig: (body: unknown) => updateThemeConfigFn(body),
    createTheme: () => Promise.resolve({}),
    deleteTheme: () => Promise.resolve({}),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'
import { THEME_SCHEDULE_STORAGE_KEY, parseLocation, readStoredThemeSchedule, scheduledMode } from '../utils/themeSchedule'

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

describe('scheduledMode with the sunrise-and-sunset source (#21097)', () => {
  const sun = (location: string) => ({ light: '07:00', dark: '19:00', source: 'sun' as const, location })
  const at = (iso: string) => new Date(iso)

  it('is light from local sunrise to sunset (London, 21 June: up 03:43, down 20:21 UTC)', () => {
    const london = sun('51.5074, -0.1278')
    expect(scheduledMode(london, at('2026-06-21T03:30:00Z'))).toBe('dark')
    expect(scheduledMode(london, at('2026-06-21T04:00:00Z'))).toBe('light')
    // 20:00 is past the 19:00 "Dark from" time: the sun wins, not the clock.
    expect(scheduledMode(london, at('2026-06-21T20:00:00Z'))).toBe('light')
    expect(scheduledMode(london, at('2026-06-21T20:40:00Z'))).toBe('dark')
  })

  it('follows the southern hemisphere, where June is winter (Sydney: down 06:53, up 20:59 UTC)', () => {
    const sydney = sun('-33.8688,151.2093')
    expect(scheduledMode(sydney, at('2026-06-21T02:00:00Z'))).toBe('light')
    expect(scheduledMode(sydney, at('2026-06-21T07:10:00Z'))).toBe('dark')
    expect(scheduledMode(sydney, at('2026-06-21T21:20:00Z'))).toBe('light')
  })

  it('stays light through a polar day and dark through a polar night (Tromsø)', () => {
    const tromso = sun('69.65, 18.96')
    for (const hour of ['00', '06', '12', '18', '23']) {
      expect(scheduledMode(tromso, at(`2026-06-21T${hour}:00:00Z`))).toBe('light')
      expect(scheduledMode(tromso, at(`2026-12-21T${hour}:00:00Z`))).toBe('dark')
    }
  })

  it('reads the instant, so a device time zone change gives the same answer', () => {
    const london = sun('51.5074, -0.1278')
    const instant = at('2026-06-21T20:00:00Z')
    const zone = process.env.TZ
    try {
      process.env.TZ = 'Australia/Sydney'
      expect(scheduledMode(london, instant)).toBe('light')
      process.env.TZ = 'America/Los_Angeles'
      expect(scheduledMode(london, instant)).toBe('light')
    } finally {
      process.env.TZ = zone
    }
  })

  it('keeps to the two times when the location is empty or not a latitude and longitude', () => {
    const noon = new Date(2026, 11, 21, 12, 0)
    const night = new Date(2026, 11, 21, 22, 0)
    for (const location of ['', 'London', '91, 0', '0, 181', '51.5', '51.5, -0.1\n']) {
      expect(parseLocation(location)).toBeNull()
      // Tromsø's polar night would be dark at noon; the times say light.
      expect(scheduledMode(sun(location), noon)).toBe('light')
      expect(scheduledMode(sun(location), night)).toBe('dark')
    }
    expect(parseLocation(' -33.87 , 151.21 ')).toEqual({ latitude: -33.87, longitude: 151.21 })
  })

  it('drops an invalid source or location read back from storage', () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '08:00', dark: '20:00', source: 'moon', location: 'nowhere' }))
    expect(readStoredThemeSchedule()).toEqual({ light: '08:00', dark: '20:00' })
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '08:00', dark: '20:00', source: 'sun', location: '69.65, 18.96' }))
    expect(readStoredThemeSchedule()).toEqual({ light: '08:00', dark: '20:00', source: 'sun', location: '69.65, 18.96' })
    localStorage.clear()
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

  it('saves the sunrise-and-sunset source with its location, and keeps the times', async () => {
    const { result } = await bootWith({})
    act(() => result.current.setSchedule({ light: '07:00', dark: '19:00', source: 'sun', location: '-33.87, 151.21' }))
    await waitFor(() => expect(updateThemeConfigFn).toHaveBeenCalledWith({
      schedule_light: '07:00', schedule_dark: '19:00', schedule_source: 'sun', schedule_location: '-33.87, 151.21',
    }))
  })

  it("adopts the workspace's sun source at boot and its location from the signed-in read", async () => {
    localStorage.setItem(THEME_SCHEDULE_STORAGE_KEY, JSON.stringify({ light: '07:00', dark: '19:00', location: '1, 2' }))
    let answer: (v: unknown) => void = () => {}
    themeConfigFn.mockReset().mockReturnValue(new Promise(resolve => { answer = resolve }))
    // Boot answers without a token, so it carries no location: this browser's copy holds.
    const { result } = await bootWith({ mode: 'schedule', schedule_source: 'sun' })
    await waitFor(() => expect(result.current.schedule).toEqual({ light: '07:00', dark: '19:00', source: 'sun', location: '1, 2' }))
    await waitFor(() => expect(themeConfigFn).toHaveBeenCalledTimes(1))
    await act(async () => { answer({ schedule_location: '69.65, 18.96' }) })
    await waitFor(() => expect(result.current.schedule).toEqual({ light: '07:00', dark: '19:00', source: 'sun', location: '69.65, 18.96' }))
    expect(updateThemeConfigFn).not.toHaveBeenCalledWith(expect.objectContaining({ schedule_location: expect.anything() }))
  })

  it('a location saved here while the read runs is not replaced by its late answer', async () => {
    let answer: (v: unknown) => void = () => {}
    themeConfigFn.mockReset().mockReturnValue(new Promise(resolve => { answer = resolve }))
    const { result } = await bootWith({ mode: 'schedule', schedule_source: 'sun' })
    await waitFor(() => expect(themeConfigFn).toHaveBeenCalledTimes(1))
    act(() => result.current.setSchedule({ light: '07:00', dark: '19:00', source: 'sun', location: '-33.87, 151.21' }))
    await act(async () => { answer({ schedule_location: '51.51, -0.13' }) })
    expect(result.current.schedule.location).toBe('-33.87, 151.21')
  })

  it('reports a failed location read, and a retry that answers clears it', async () => {
    themeConfigFn.mockReset().mockRejectedValue(new Error('offline'))
    const { result } = await bootWith({ mode: 'schedule', schedule_source: 'sun' })
    await waitFor(() => expect(result.current.scheduleLocationLoadFailed).toBe(true), { timeout: 10_000 })
    themeConfigFn.mockReset().mockResolvedValue({ schedule_location: '69.65, 18.96' })
    act(() => result.current.retryScheduleLocation())
    await waitFor(() => expect(result.current.scheduleLocationLoadFailed).toBe(false))
    await waitFor(() => expect(result.current.schedule.location).toBe('69.65, 18.96'))
  }, 15_000)

  it('a time or source edit from a browser without the location never writes over it, and the read still fills it in', async () => {
    themeConfigFn.mockReset().mockResolvedValue({ schedule_location: '51.51, -0.13' })
    const { result } = await bootWith({ mode: 'schedule', schedule_source: 'times', schedule_light: '07:00', schedule_dark: '19:00' })
    await waitFor(() => expect(result.current.schedule).toEqual({ light: '07:00', dark: '19:00', source: 'times' }))
    act(() => result.current.setSchedule({ ...result.current.schedule, light: '06:00' }))
    act(() => result.current.setSchedule({ ...result.current.schedule, source: 'sun' }))
    await waitFor(() => expect(result.current.schedule.location).toBe('51.51, -0.13'))
    for (const [body] of updateThemeConfigFn.mock.calls) expect(body).not.toHaveProperty('schedule_location')
    // An explicit clear is a location edit, so it is sent.
    act(() => result.current.setSchedule({ ...result.current.schedule, location: undefined }))
    await waitFor(() => expect(updateThemeConfigFn).toHaveBeenLastCalledWith({ schedule_light: '06:00', schedule_dark: '19:00', schedule_source: 'sun', schedule_location: '' }))
  })

  it('a refused location save is sent again by the next save, until one succeeds', async () => {
    const { result } = await bootWith({ mode: 'schedule', schedule_source: 'times' })
    updateThemeConfigFn.mockReset().mockRejectedValueOnce(new Error('refused')).mockResolvedValue({})
    act(() => result.current.setSchedule({ ...result.current.schedule, source: 'sun', location: '-33.87, 151.21' }))
    await waitFor(() => expect(result.current.scheduleSaveFailed).toBe(true))
    act(() => result.current.setSchedule({ ...result.current.schedule }))
    await waitFor(() => expect(updateThemeConfigFn).toHaveBeenCalledTimes(2))
    expect(updateThemeConfigFn.mock.calls[1][0]).toMatchObject({ schedule_location: '-33.87, 151.21' })
    await waitFor(() => expect(result.current.scheduleSaveFailed).toBe(false))
    act(() => result.current.setSchedule({ ...result.current.schedule, light: '06:00' }))
    await waitFor(() => expect(updateThemeConfigFn).toHaveBeenCalledTimes(3))
    expect(updateThemeConfigFn.mock.calls[2][0]).not.toHaveProperty('schedule_location')
  })

  it('does not ask for the location when the schedule follows the two times', async () => {
    themeConfigFn.mockReset()
    const { result } = await bootWith({ mode: 'schedule', schedule_light: '06:00', schedule_dark: '18:00' })
    await waitFor(() => expect(result.current.schedule).toEqual({ light: '06:00', dark: '18:00' }))
    expect(themeConfigFn).not.toHaveBeenCalled()
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
})
