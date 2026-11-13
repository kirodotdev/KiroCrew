/**
 * Settings → Display → Theme: the schedule rows write the device-local schedule,
 * and the sunrise/sunset method works from a typed location without any network
 * call or geolocation prompt.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ThemeScheduleSettings } from './ThemeScheduleSettings'
import { readThemeSchedule, writeThemeSchedule } from '../../hooks/useThemeSchedule'
import { DEFAULT_THEME_SCHEDULE, THEME_SCHEDULE_STORAGE_KEY } from '../../utils/themeSchedule'
import { i18nT } from '../../i18n/t'

const k = (key: string) => i18nT(`pages.settings.displayPanel.${key}`)

describe('ThemeScheduleSettings', () => {
  beforeEach(() => {
    localStorage.removeItem(THEME_SCHEDULE_STORAGE_KEY)
    writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.removeItem(THEME_SCHEDULE_STORAGE_KEY)
  })

  it('says in the always-visible text that it switches the light and dark themes', () => {
    render(<ThemeScheduleSettings />)
    // The UX review read the row cold: the label and the line under it, not the ⓘ tooltip.
    expect(screen.getByRole('switch', { name: /light and dark/i })).toBeTruthy()
    const description = screen.getByText(/Switches between the light and dark themes automatically/)
    expect(description.textContent).toMatch(/time zone/)
  })

  it('turns the schedule on and shows the method choices', () => {
    render(<ThemeScheduleSettings />)
    expect(screen.queryByText(k('theme_schedule_sun'))).toBeNull()
    fireEvent.click(screen.getByRole('switch', { name: k('theme_schedule') }))
    expect(readThemeSchedule().enabled).toBe(true)
    expect(JSON.parse(localStorage.getItem(THEME_SCHEDULE_STORAGE_KEY)!).enabled).toBe(true)
    expect(screen.getByText(k('theme_schedule_sun'))).toBeTruthy()
    expect(screen.getByText(k('theme_schedule_fixed'))).toBeTruthy()
  })

  it('sets fixed light and dark times', () => {
    writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE, enabled: true })
    render(<ThemeScheduleSettings />)
    fireEvent.click(screen.getByText(k('theme_schedule_fixed')))
    expect(readThemeSchedule().method).toBe('fixed')
    fireEvent.change(screen.getByLabelText(k('theme_schedule_light_at')), { target: { value: '06:45' } })
    fireEvent.change(screen.getByLabelText(k('theme_schedule_dark_at')), { target: { value: '21:30' } })
    expect(readThemeSchedule()).toMatchObject({ lightAt: '06:45', darkAt: '21:30', pickedAt: null })
  })

  it('computes sunrise and sunset from a typed location, with no network or geolocation', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const geolocation = { getCurrentPosition: vi.fn(), watchPosition: vi.fn() }
    Object.defineProperty(navigator, 'geolocation', { value: geolocation, configurable: true })
    writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE, enabled: true, method: 'sun' })
    render(<ThemeScheduleSettings />)
    expect(screen.getByText(k('theme_schedule_location_missing'))).toBeTruthy()

    fireEvent.change(screen.getByLabelText(k('theme_schedule_latitude')), { target: { value: '51.5' } })
    fireEvent.change(screen.getByLabelText(k('theme_schedule_longitude')), { target: { value: '-0.1' } })
    expect(readThemeSchedule()).toMatchObject({ latitude: 51.5, longitude: -0.1 })
    expect(screen.queryByText(k('theme_schedule_location_missing'))).toBeNull()

    expect(fetchSpy).not.toHaveBeenCalled()
    expect(geolocation.getCurrentPosition).not.toHaveBeenCalled()
    expect(geolocation.watchPosition).not.toHaveBeenCalled()
  })

  it('does not store an out-of-range latitude', () => {
    writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE, enabled: true, method: 'sun' })
    render(<ThemeScheduleSettings />)
    fireEvent.change(screen.getByLabelText(k('theme_schedule_latitude')), { target: { value: '120' } })
    expect(readThemeSchedule().latitude).toBeNull()
    expect(screen.getByText(k('theme_schedule_latitude_invalid'))).toBeTruthy()
  })

  it('says so when the browser refuses to save, and keeps the typed location', () => {
    writeThemeSchedule({ ...DEFAULT_THEME_SCHEDULE, enabled: true, method: 'sun' })
    const original = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === THEME_SCHEDULE_STORAGE_KEY) throw new DOMException('full', 'QuotaExceededError')
      return original.call(this, key, value)
    })
    render(<ThemeScheduleSettings />)
    const field = screen.getByLabelText(k('theme_schedule_latitude')) as HTMLInputElement
    fireEvent.change(field, { target: { value: '51.5' } })
    fireEvent.blur(field)
    expect(screen.getByRole('alert').textContent).toContain(i18nT('components.themeExperienceLayer.save_failed_storage'))
    expect(field.value).toBe('51.5')
    expect(readThemeSchedule().latitude).toBeNull()
  })

  it('says so when turning the schedule on cannot be saved', () => {
    const original = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === THEME_SCHEDULE_STORAGE_KEY) throw new DOMException('blocked', 'SecurityError')
      return original.call(this, key, value)
    })
    render(<ThemeScheduleSettings />)
    fireEvent.click(screen.getByRole('switch', { name: k('theme_schedule') }))
    expect(readThemeSchedule().enabled).toBe(false)
    expect(screen.getByRole('alert').textContent).toContain(i18nT('components.themeExperienceLayer.save_failed_storage'))
  })
})
