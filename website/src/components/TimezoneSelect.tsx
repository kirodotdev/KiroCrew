import { useMemo } from 'react'
import { formatTzOffset } from '../utils/tz'
import SearchableSelect from './SearchableSelect'

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

/** `formatTzOffset` delegates to `Intl.DateTimeFormat`, which throws
 *  `RangeError` on a zone the runtime does not know. `value` comes from
 *  localStorage and the zone list from the host, so a stale or retired id
 *  (Factory/Legacy, a zone dropped by a browser update) would otherwise take
 *  the whole Schedule page down. Degrade to no offset instead. */
function safeOffset(tz: string): string | undefined {
  try {
    return formatTzOffset(tz)
  } catch {
    return undefined
  }
}

/** Dropdown picker for choosing the render timezone for the Schedule
 *  page. Persisted by the parent via `localStorage`. */
export default function TimezoneSelect({ value, onChange, className, id }: Props) {
  const options = useMemo(() => {
    const all = allTimezones()
    // De-duplicate while keeping `value` and `COMMON_TZS` ordered first.
    const ordered = [value, ...COMMON_TZS, ...all]
    return [...new Set(ordered.filter(Boolean))].map(tz => {
      const offset = safeOffset(tz)
      return {
        value: tz,
        label: tz,
        // `formatTzOffset('UTC')` is itself "UTC", so showing both would render
        // "UTC (UTC)". Drop a sublabel that just repeats the zone name.
        sublabel: offset === tz ? undefined : offset,
        // Let a query match the city without its underscore and the region
        // without its slash, so "los angeles" and "america los" both hit.
        keywords: tz.replace(/[_/]/g, ' '),
      }
    })
  }, [value])

  return (
    <SearchableSelect
      id={id}
      options={options}
      value={value}
      onChange={onChange}
      className={className}
      aria-label={i18nT('components.timezoneSelect.render_timezone')}
    />
  )
}
