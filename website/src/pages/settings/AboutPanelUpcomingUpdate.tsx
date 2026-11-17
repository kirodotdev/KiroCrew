import { ArrowUp, ExternalLink } from 'lucide-react'
import { useAppSelector } from '../../store'
import { i18nT } from '../../i18n/t'
import { fmtDate } from '../../i18n/format'
import { dayToDate, upcomingUpdate } from './upcomingUpdate'

/**
 * The advance notice of the next update, drawn inline under About > Updates.
 * Never a pop-up (docs/decisions/2026-11-10-the-update-preview-lives-inline-in-about-under-update.md).
 *
 * It states a fact and offers no control: the Update or Restart control the card
 * above already carries stays the only one. It also never promises a time the
 * update installs by itself: it says the update takes effect on the next restart
 * once released. It shows an announced build only; a build the feed already offers
 * is the update card's to describe. Draws nothing when there is no notice.
 */
export function AboutPanelUpcomingUpdate() {
  const gateway = useAppSelector(s => s.dashboard.status?.update_upcoming)
  const upcoming = upcomingUpdate(gateway)
  if (!upcoming) return null
  const title = i18nT('pages.settings.aboutPanel.upcoming_update_title')
  const when = dayToDate(upcoming.date)
  const date = when ? fmtDate(when, { timeZone: 'UTC' }) : ''
  return (
    <section
      className="mt-3 p-3 bg-bg rounded-lg border border-border flex flex-col gap-2"
      aria-label={title}
      data-testid="upcoming-update"
      data-state={upcoming.state}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5 min-w-0">
          <span className="text-[13px] font-medium text-text flex items-center gap-1.5">
            <ArrowUp size={13} className="lucide-inline text-accent" aria-hidden />
            {title}
          </span>
          <span className="text-[12px] text-muted" data-testid="upcoming-update-meta">
            {i18nT('pages.settings.aboutPanel.upcoming_update_version', { version: upcoming.version })}
            {date && ` · ${i18nT('pages.settings.aboutPanel.upcoming_update_expected_on', { date })}`}
          </span>
        </div>
        <span
          className="shrink-0 text-[11.5px] font-semibold rounded-full px-2 py-0.5 text-muted bg-bg-accent"
          data-testid="upcoming-update-state"
        >
          {i18nT('pages.settings.aboutPanel.upcoming_update_announced')}
        </span>
      </div>
      {upcoming.highlights.length > 0 && (
        <ul
          className="list-disc pl-5 text-[12px] text-text flex flex-col gap-0.5"
          aria-label={i18nT('pages.settings.aboutPanel.upcoming_update_highlights')}
          data-testid="upcoming-update-highlights"
        >
          {upcoming.highlights.map(line => <li key={line}>{line}</li>)}
        </ul>
      )}
      {upcoming.notesUrl && (
        <a
          href={upcoming.notesUrl}
          target="_blank"
          rel="noreferrer noopener"
          className="text-[12px] text-accent hover:underline inline-flex items-center gap-1.5 self-start"
          data-testid="upcoming-update-notes"
        >
          {i18nT('pages.settings.aboutPanel.upcoming_update_notes_link')}
          <ExternalLink size={11} className="lucide-inline" aria-hidden />
        </a>
      )}
      <span className="text-[12px] text-muted" data-testid="upcoming-update-takes-effect">
        {i18nT('pages.settings.aboutPanel.upcoming_update_announced_takes_effect_on_restart')}
      </span>
    </section>
  )
}
