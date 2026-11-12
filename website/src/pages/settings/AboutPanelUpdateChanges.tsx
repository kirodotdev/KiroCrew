import { useMemo } from 'react'
import { ExternalLink, RefreshCw } from 'lucide-react'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'
import { changeEntries } from './updateChanges'

/** How many changes the list names before it folds the rest into a count. */
export const UPDATE_CHANGES_SHOWN = 5

const RELEASES_URL = 'https://github.com/kirodotdev/KiroCrew/releases'

/**
 * "What you'd get": the changes an available update brings, under its Update
 * control in About > Updates.
 *
 * `notes` is the changelog text the update itself carries (see updateChanges.ts).
 * A feed-installed gateway's check carries none, so an empty `notes` says the
 * notes are not shown here and links the release on GitHub, rather than
 * claiming the update changes nothing. A git checkout's check carries them, but
 * the update can be known from the background check alone: `onLoad` is that
 * case, and offers to run the check that brings them.
 */
export function AboutPanelUpdateChanges({ notes, version, sinceBuild = false, onLoad, loading = false }: {
  notes: string
  version: string
  /**
   * The notes span every release since the running build (a checkout's
   * `CHANGELOG.md` diff). False for the desktop feed, whose notes are the
   * target version's own section only.
   */
  sinceBuild?: boolean
  /** Runs the check that returns this update's notes; absent once it has run. */
  onLoad?: () => void
  loading?: boolean
}) {
  const entries = useMemo(() => changeEntries(notes), [notes])
  // Only a bare release has a tag of that exact name; a nightly stamp has none.
  const releaseUrl = /^[0-9]+(\.[0-9]+)*$/.test(version) ? `${RELEASES_URL}/tag/v${version}` : RELEASES_URL
  if (entries.length === 0 && onLoad) {
    return (
      <button
        type="button"
        onClick={onLoad}
        disabled={loading}
        data-testid="update-changes-load"
        className="self-start text-[12px] text-accent hover:underline inline-flex items-center gap-1.5 bg-transparent border-none p-0 cursor-pointer disabled:cursor-default disabled:opacity-60"
      >
        {loading && <RefreshCw size={12} className="lucide-inline animate-spin" aria-hidden />}
        {i18nT('pages.settings.aboutPanel.what_you_d_get_load')}
      </button>
    )
  }
  if (entries.length === 0) {
    return (
      <p className="text-[12px] text-muted" data-testid="update-changes-empty">
        {i18nT('pages.settings.aboutPanel.what_you_d_get_no_entries')}{' '}
        <a
          href={releaseUrl}
          target="_blank"
          rel="noreferrer noopener"
          className="text-accent hover:underline inline-flex items-center gap-1"
        >
          {i18nT('pages.settings.aboutPanel.what_you_d_get_view_on_github')}
          <ExternalLink size={11} className="lucide-inline" aria-hidden />
        </a>
      </p>
    )
  }
  const hidden = entries.length - UPDATE_CHANGES_SHOWN
  const title = sinceBuild
    ? i18nT('pages.settings.aboutPanel.what_you_d_get', { count: entries.length })
    : i18nT('pages.settings.aboutPanel.what_you_d_get_in_version', { count: entries.length, version })
  return (
    <section className="flex flex-col gap-1.5" aria-label={title} data-testid="update-changes">
      <span className="text-[12px] font-medium text-text">{title}</span>
      <ul className="list-disc list-inside text-[12px] text-text flex flex-col gap-0.5" data-testid="update-changes-list">
        {entries.slice(0, UPDATE_CHANGES_SHOWN).map((entry, i) => (
          // Index in the key: two changes may share a lead ("Settings").
          <li key={`${i}-${entry}`} className="truncate">{entry}</li>
        ))}
      </ul>
      {hidden > 0 && (
        <span className="text-[12px] text-muted" data-testid="update-changes-more">
          {i18nT('pages.settings.aboutPanel.what_you_d_get_more', { count: hidden })}
        </span>
      )}
      <details className="text-[12px]">
        <summary className="cursor-pointer text-accent hover:underline w-fit">
          {i18nT('pages.settings.aboutPanel.what_you_d_get_read_all')}
        </summary>
        <div className="mt-2 p-2.5 bg-card rounded-md border border-border max-h-60 overflow-y-auto text-text">
          <MarkdownRenderer content={notes} />
        </div>
      </details>
    </section>
  )
}
