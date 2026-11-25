/**
 * Settings > Display, Mode = Scheduled: how it switches. Fixed times uses the
 * two time fields below it; Sunrise and sunset follows the sun at a typed
 * latitude and longitude, kept in this browser only (`setSunSchedule`), so it
 * never writes the workspace. No geolocation call and no online lookup.
 */
import { useState } from 'react'
import { SettingsButtonGroup, SettingsInput } from '../../components/settings'
import { useTheme } from '../../hooks/useTheme'
import { fmtTime } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { isLatitude, isLongitude, sunTimes } from '../../utils/themeSchedule'

type Coordinate = 'latitude' | 'longitude'

export function ThemeScheduleMethodSetting() {
  const { schedule, setSunSchedule } = useTheme()
  // Saved on blur like the times; an empty, unreadable or out-of-range entry is dropped and the field shows the stored value.
  const [draft, setDraft] = useState<Partial<Record<Coordinate, string>>>({})
  const commit = (field: Coordinate) => {
    const text = draft[field]
    setDraft(d => ({ ...d, [field]: undefined }))
    if (text === undefined || text.trim() === '') return
    const value = Number(text)
    if (!(field === 'latitude' ? isLatitude(value) : isLongitude(value))) return
    setSunSchedule(field === 'latitude' ? { latitude: value } : { longitude: value })
  }
  const { latitude, longitude } = schedule
  const today = isLatitude(latitude) && isLongitude(longitude) ? sunTimes(new Date(), latitude, longitude) : null
  const sun = schedule.method === 'sun'
  return (
    <>
      <SettingsButtonGroup label={i18nT('pages.settings.displayPanel.theme_schedule_method')} value={sun ? 'sun' : 'fixed'}
        options={[
          { value: 'fixed', label: i18nT('pages.settings.displayPanel.theme_schedule_method_fixed') },
          { value: 'sun', label: i18nT('pages.settings.displayPanel.theme_schedule_method_sun') },
        ]}
        onChange={v => setSunSchedule({ method: v === 'sun' ? 'sun' : undefined })} />
      {sun && (
        <>
          <SettingsInput type="number" label={i18nT('pages.settings.displayPanel.theme_schedule_latitude')} hint={i18nT('pages.settings.displayPanel.theme_schedule_latitude_hint')} min={-90} max={90} step={0.01}
            value={draft.latitude ?? (latitude ?? '').toString()} onChange={v => setDraft(d => ({ ...d, latitude: v }))} onBlur={() => commit('latitude')} />
          <SettingsInput type="number" label={i18nT('pages.settings.displayPanel.theme_schedule_longitude')} hint={i18nT('pages.settings.displayPanel.theme_schedule_longitude_hint')} min={-180} max={180} step={0.01}
            value={draft.longitude ?? (longitude ?? '').toString()} onChange={v => setDraft(d => ({ ...d, longitude: v }))} onBlur={() => commit('longitude')} />
          <p role="status" className="text-[12px] text-muted" data-testid="theme-schedule-sun-status">
            {today === null ? i18nT('pages.settings.displayPanel.theme_schedule_sun_needs_location')
              : today.kind === 'polar-day' ? i18nT('pages.settings.displayPanel.theme_schedule_sun_polar_day')
              : today.kind === 'polar-night' ? i18nT('pages.settings.displayPanel.theme_schedule_sun_polar_night')
              : i18nT('pages.settings.displayPanel.theme_schedule_sun_today', { sunrise: fmtTime(today.sunrise), sunset: fmtTime(today.sunset) })}
          </p>
          <p className="text-[12px] text-muted">{i18nT('pages.settings.displayPanel.theme_schedule_sun_kept_here')}</p>
        </>
      )}
    </>
  )
}
