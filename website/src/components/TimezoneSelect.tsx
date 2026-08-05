import { useMemo } from 'react'
import { formatTzOffset } from '../utils/tz'

import { i18nT } from '../i18n/t'
/** A curated short list of commonly used timezones, used as
 *  a fast-pick set. Full IANA list loaded on demand from the browser. */
const COMMON_TZS = [
  'America/Los_Angeles',
  'America/Denver',
  'America/Chicago',
  'America/New_York',
  'America/Sao_Paulo',
  'Europe/London',
  'Europe/Dublin',
  'Europe/Berlin',
  'Europe/Paris',
  'Asia/Kolkata',
  'Asia/Dubai',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Australia/Sydney',
  'Pacific/Auckland',
  'UTC',
]

interface Props {
  value: string
  onChange: (tz: string) => void
  className?: string
  id?: string
}

function allTimezones(): string[] {
  // `Intl.supportedValuesOf` is standard since 2022 but may be absent on
  // older Safari — fall back to the curated list if missing.
  const maybeSupported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
    .supportedValuesOf
  if (typeof maybeSupported === 'function') {
    try {
      return maybeSupported('timeZone')
    } catch {
      // fall through
    }
  }
  return COMMON_TZS
}

/** Dropdown picker for choosing the render timezone for the Schedule
 *  page. Persisted by the parent via `localStorage`. */
export default function TimezoneSelect({ value, onChange, className, id }: Props) {
  const allTzs = useMemo(() => {
    const all = allTimezones()
    // De-duplicate while keeping `value` and `COMMON_TZS` ordered first.
    const ordered = [value, ...COMMON_TZS, ...all]
    return [...new Set(ordered.filter(Boolean))]
  }, [value])

  const selectCls =
    'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none cursor-pointer transition-colors focus-ring'

  return (
    <select
      id={id}
      value={value}
      onChange={e => onChange(e.target.value)}
      className={`${selectCls} ${className || ''}`.trim()}
      aria-label={i18nT('components.timezoneSelect.render_timezone')}
    >
      {allTzs.map(tz => (
        <option key={tz} value={tz}>
          {tz} ({formatTzOffset(tz)})
        </option>
      ))}
    </select>
  )
}
