/**
 * Settings → Display → Theme: the "Switch on a schedule" rows, under the Mode
 * picker. The schedule is device-local (see `useThemeSchedule`), so these rows
 * write localStorage and carry a `settingId`, not a gateway `configKey`.
 */

import { useId, useState } from 'react'
import { Sunrise, Clock } from 'lucide-react'
import { SettingsButtonGroup, SettingsInput, SettingsToggle } from '../../components/settings'
import { useThemeSchedule } from '../../hooks/useThemeSchedule'
import { i18nT } from '../../i18n/t'
import { fmtTime } from '../../i18n/format'
import {
  hasSunLocation,
  isValidClockTime,
  isValidLatitude,
  isValidLongitude,
  sunTimes,
  type ThemeScheduleMethod,
} from '../../utils/themeSchedule'

function deviceTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/**
 * A typed coordinate field's state: '' clears the stored value, a number in range
 * sets it, and anything else is held as a draft (and flagged) without being stored.
 */
function useCoordinate(value: number | null, isValid: (v: number | null) => v is number, commit: (v: number | null) => void) {
  const [draft, setDraft] = useState<string | null>(null)
  const errorId = useId()
  const text = draft ?? (value === null ? '' : String(value))
  const parsed = text.trim() === '' ? null : Number(text)
  return {
    text,
    errorId,
    invalid: parsed !== null && !isValid(parsed),
    onChange: (v: string) => {
      setDraft(v)
      const n = v.trim() === '' ? null : Number(v)
      if (n === null || isValid(n)) commit(n)
    },
    onBlur: () => setDraft(null),
  }
}

export function ThemeScheduleSettings() {
  const [schedule, update] = useThemeSchedule()
  const latitude = useCoordinate(schedule.latitude, isValidLatitude, v => update({ latitude: v }))
  const longitude = useCoordinate(schedule.longitude, isValidLongitude, v => update({ longitude: v }))
  const timeZone = deviceTimeZone()
  const useSun = schedule.method === 'sun'
  const located = hasSunLocation(schedule)
  // Today's sun events, for the line that shows the user what the schedule will do.
  const today = useSun && located ? sunTimes(new Date(), schedule.latitude as number, schedule.longitude as number) : null
  const showFixedTimes = !useSun || !located

  return (
    <>
      <SettingsToggle
        label={i18nT('pages.settings.displayPanel.theme_schedule')}
        description={i18nT('pages.settings.displayPanel.theme_schedule_timezone', { timeZone })}
        hint={i18nT('pages.settings.displayPanel.theme_schedule_hint')}
        settingId="display.theme-schedule"
        checked={schedule.enabled}
        onChange={enabled => update({ enabled })}
      />
      {schedule.enabled && (
        <div className="flex flex-col gap-1 pl-3 border-l border-border">
          <SettingsButtonGroup
            label={i18nT('pages.settings.displayPanel.theme_schedule_method')}
            value={schedule.method}
            options={[
              { value: 'sun', label: i18nT('pages.settings.displayPanel.theme_schedule_sun'), icon: <Sunrise className="w-3.5 h-3.5" /> },
              { value: 'fixed', label: i18nT('pages.settings.displayPanel.theme_schedule_fixed'), icon: <Clock className="w-3.5 h-3.5" /> },
            ]}
            onChange={v => update({ method: v as ThemeScheduleMethod })}
          />
          {useSun && (
            <>
              <div className="text-[12px] text-muted">{i18nT('pages.settings.displayPanel.theme_schedule_location_hint')}</div>
              <SettingsInput
                label={i18nT('pages.settings.displayPanel.theme_schedule_latitude')}
                type="number"
                step={0.1}
                value={latitude.text}
                onChange={latitude.onChange}
                onBlur={latitude.onBlur}
                invalidMessageId={latitude.invalid ? latitude.errorId : undefined}
                after={latitude.invalid
                  ? <div id={latitude.errorId} className="text-[12px] text-danger">{i18nT('pages.settings.displayPanel.theme_schedule_latitude_invalid')}</div>
                  : undefined}
              />
              <SettingsInput
                label={i18nT('pages.settings.displayPanel.theme_schedule_longitude')}
                type="number"
                step={0.1}
                value={longitude.text}
                onChange={longitude.onChange}
                onBlur={longitude.onBlur}
                invalidMessageId={longitude.invalid ? longitude.errorId : undefined}
                after={longitude.invalid
                  ? <div id={longitude.errorId} className="text-[12px] text-danger">{i18nT('pages.settings.displayPanel.theme_schedule_longitude_invalid')}</div>
                  : undefined}
              />
              <div className="text-[12px] text-muted" aria-live="polite">
                {!today && i18nT('pages.settings.displayPanel.theme_schedule_location_missing')}
                {today?.kind === 'normal' && i18nT('pages.settings.displayPanel.theme_schedule_today', {
                  sunrise: fmtTime(today.sunrise),
                  sunset: fmtTime(today.sunset),
                })}
                {today?.kind === 'polar-day' && i18nT('pages.settings.displayPanel.theme_schedule_polar_day')}
                {today?.kind === 'polar-night' && i18nT('pages.settings.displayPanel.theme_schedule_polar_night')}
              </div>
            </>
          )}
          {showFixedTimes && (
            <>
              <SettingsInput
                label={i18nT('pages.settings.displayPanel.theme_schedule_light_at')}
                type="time"
                value={schedule.lightAt}
                onChange={v => { if (isValidClockTime(v)) update({ lightAt: v }) }}
              />
              <SettingsInput
                label={i18nT('pages.settings.displayPanel.theme_schedule_dark_at')}
                type="time"
                value={schedule.darkAt}
                onChange={v => { if (isValidClockTime(v)) update({ darkAt: v }) }}
              />
              {schedule.lightAt === schedule.darkAt && (
                <div className="text-[12px] text-danger" aria-live="polite">{i18nT('pages.settings.displayPanel.theme_schedule_same_times')}</div>
              )}
            </>
          )}
        </div>
      )}
    </>
  )
}
