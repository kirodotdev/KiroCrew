/**
 * The view model behind About's "Upcoming update" block.
 *
 * The gateway's `update_upcoming`: a published advance notice of a version the
 * release feed does not offer yet. Once a build is offered, the update card
 * already describes it and this view model has nothing to add. Plain string
 * handling only.
 */

export type UpcomingUpdate = {
  state: 'announced'
  version: string
  /** `YYYY-MM-DD`, or "" when unknown. */
  date: string
  highlights: string[]
  /** An `https://` link to the full notes, or "". */
  notesUrl: string
}

export type GatewayUpcoming = {
  state: 'announced'
  version: string
  date: string
  highlights: string[]
  notes_url: string
} | null | undefined

const MAX_HIGHLIGHTS = 3

/** `YYYY-MM-DD` as a UTC date, so the host time zone cannot move the day. */
export function dayToDate(day: string): Date | null {
  const parts = day.slice(0, 10).split('-').map(Number)
  if (parts.length !== 3 || parts.some(n => !Number.isInteger(n))) return null
  const [year, month, dayOfMonth] = parts
  if (month < 1 || month > 12 || dayOfMonth < 1 || dayOfMonth > 31) return null
  return new Date(Date.UTC(year, month - 1, dayOfMonth))
}

/** The link only when it is a plain `https://` URL; anything else is not drawn. */
export function safeNotesUrl(url: string | undefined): string {
  if (!url || !url.startsWith('https://') || url.length <= 'https://'.length) return ''
  return url.split('').some(ch => ch.trim() === '') ? '' : url
}

export function upcomingUpdate(gateway: GatewayUpcoming): UpcomingUpdate | null {
  if (!gateway || !gateway.version) return null
  return {
    state: gateway.state,
    version: gateway.version,
    date: gateway.date,
    highlights: gateway.highlights.slice(0, MAX_HIGHLIGHTS),
    notesUrl: safeNotesUrl(gateway.notes_url),
  }
}
