/**
 * Settings → Releases: the changelog archive, as a version list plus a detail pane.
 *
 * ## Why this is not inside About
 *
 * About states the identity of *this install* — version, licence, platform,
 * channel, update controls — and its content is bounded: it is one screen today
 * and one screen in five years. A changelog grows without limit. Nesting the
 * unbounded thing inside the bounded one is what made About's disclosure an
 * ever-growing scroll blob, so the archive is a sibling of About rather than a
 * child of it, and About links here.
 *
 * ## Why the list is short
 *
 * Rows come from {@link https://github.com/kirodotdev/KiroCrew | CHANGELOG.md}'s
 * sections plus the release the running build belongs to — nothing else. A
 * version that shipped without a section is deliberately absent, because a row
 * that cannot say anything is indistinguishable from a broken one. The running
 * build's release is the single exception: "what am I on?" is the question this
 * page is opened to answer. `kiro_crew/changelog.py` owns that rule.
 *
 * ## Why prereleases have no rows of their own
 *
 * `0.2.0-rc.1` and `0.2.0-nightly.<stamp>` are drafts of 0.2.0, not releases.
 * Both collapse onto the single `0.2.0` row, flagged in-progress, so one
 * mechanism serves both channels, nightly does not add ~4 rows a day forever,
 * and the list does not move when 0.2.0 is promoted — the row is already there
 * and merely loses the flag. Which RC you are on is stated in the detail pane.
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CircleDot, FileText, GitCommitHorizontal, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import { sanitize } from '../../api/helpers'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import Clickable from '../../components/Clickable'
import { i18nT } from '../../i18n/t'

/** Mirrors `kiro_crew.changelog.Release`. */
interface Release {
  version: string
  date: string
  body: string
  is_current: boolean
  in_progress: boolean
}

interface ReleasesPayload {
  current_version: string
  releases: Release[]
  stale: boolean
}

const REPO = 'https://github.com/kirodotdev/KiroCrew'

/** Small-print state note for a row, or `''` when the row needs none.
 *
 * Deliberately never the running build's full version. A prerelease stamp is
 * ~30 characters (`0.2.0-nightly.20260806t065257`) and the rail is 192px, so it
 * would truncate to something useless ("You are on 0.2.0-nigh…"). The exact
 * build is named in the detail pane, where there is room for it.
 *
 * An in-progress row returns `''`: its badge already states the state, and
 * repeating it underneath said the same thing three times on one screen (badge,
 * note, and the detail heading).
 */
function rowNote(r: Release): string {
  if (r.in_progress) return ''
  if (r.date) return r.date
  if (r.is_current) return i18nT('pages.settings.releases.row_current_no_notes')
  return i18nT('pages.settings.releases.row_no_notes')
}

export default function ReleasesPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['releases'],
    queryFn: () => api.releases() as Promise<ReleasesPayload>,
  })

  // Memoised so the `?? []` fallback does not mint a new array identity on
  // every render and re-fire the selection effect below.
  const releases = useMemo(() => data?.releases ?? [], [data?.releases])
  const [selected, setSelected] = useState<string>('')

  // Default selection is the release the running build belongs to — for a
  // prerelease that resolves to the in-progress row, for a stable build to its
  // own. Falling back to the newest row keeps the pane populated when the
  // running version cannot be matched at all.
  useEffect(() => {
    if (selected || releases.length === 0) return
    setSelected((releases.find(r => r.is_current) ?? releases[0]).version)
  }, [releases, selected])

  const active = useMemo(
    () => releases.find(r => r.version === selected) ?? releases[0],
    [releases, selected],
  )
  const safeBody = useMemo(() => (active?.body ? sanitize(active.body) : ''), [active?.body])

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-muted">
        <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
        {i18nT('pages.settings.releases.loading')}
      </div>
    )
  }
  if (releases.length === 0) {
    // A fetch failure and an archive with nothing in it are different answers,
    // and saying "not available in this build" for a 500 sends the reader to
    // look for a build problem that does not exist. `isError` is only reached
    // with no cached entries: a failed REFETCH keeps `data`, and the archive is
    // a static document, so stale-but-present beats both messages.
    if (isError) {
      return <div className="p-6 text-sm text-muted">{i18nT('pages.settings.releases.load_failed')}</div>
    }
    return <div className="p-6 text-sm text-muted">{i18nT('pages.settings.releases.unavailable')}</div>
  }

  // The newest released row that is not the one being viewed -- the base of the
  // "what changed since" range below. Skipping `active` matters for a RELEASED
  // version that shipped without notes: it is not in progress, so it would
  // otherwise select itself and the range would collapse to a bare commit list.
  const prevStable = releases.find(r => !r.in_progress && r.version !== active?.version)

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {/* No title here: `SidePanelLayout` already renders the tab's label and
          description above the panel, as it does for every other Settings tab.
          Repeating them inside said "Releases / Release notes for each version"
          twice on one screen. */}

      {/* The archive is read from this install, never the network, so a
          prerelease build shows it as of its branch point. Said out loud
          instead of letting the list look complete when it is not. */}
      {data?.stale && (
        <p className="rounded-lg bg-bg-accent px-3 py-2 text-xs text-muted">
          {i18nT('pages.settings.releases.stale_note', { version: data.current_version })}
        </p>
      )}

      <div className="flex min-h-0 flex-1 gap-5">
        <nav
          aria-label={i18nT('pages.settings.releases.list_label')}
          className="w-48 shrink-0 space-y-1 overflow-y-auto border-r border-border pr-3"
        >
          {releases.map(r => {
            const isActive = r.version === active?.version
            return (
              <Clickable
                key={r.version}
                onClick={() => setSelected(r.version)}
                aria-current={isActive ? 'true' : undefined}
                className={
                  'block w-full rounded-lg px-3 py-2 text-left ' +
                  // INSET ring, as in SearchResultsList: the row is flush with
                  // the top and left edges of a `overflow-y-auto` column, so an
                  // outside ring is drawn 1px beyond the scroll container's
                  // padding box and the top edge gets clipped away entirely.
                  (isActive ? 'bg-accent-subtle ring-1 ring-inset ring-accent' : 'hover:bg-bg-hover')
                }
              >
                <span className="flex items-center gap-1.5">
                  <span className="text-sm font-semibold tabular-nums">{r.version}</span>
                  {r.in_progress && (
                    <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-accent-fg">
                      {i18nT('pages.settings.releases.badge_in_progress')}
                    </span>
                  )}
                  {r.is_current && !r.in_progress && (
                    <CircleDot className="lucide-inline text-accent" aria-hidden="true" />
                  )}
                </span>
                {rowNote(r) && (
                  <span className="block text-xs text-muted tabular-nums">{rowNote(r)}</span>
                )}
              </Clickable>
            )
          })}
        </nav>

        <article className="min-w-0 flex-1 overflow-y-auto">
          <header className="mb-4">
            <h3 className="flex items-center gap-2 text-xl font-semibold">
              {active?.version}
              {active?.in_progress && (
                <span className="text-xs font-normal text-muted">
                  {i18nT('pages.settings.releases.detail_unreleased')}
                </span>
              )}
            </h3>
            <p className="text-xs text-muted tabular-nums">
              {active?.in_progress
                ? i18nT('pages.settings.releases.detail_on_prerelease', { version: data!.current_version })
                : active?.date || i18nT('pages.settings.releases.detail_no_date')}
            </p>
          </header>

          {safeBody ? (
            <MarkdownRenderer content={safeBody} />
          ) : (
            // No section for this version. The row stays selectable precisely so
            // the absence can be explained here and a real path offered —
            // a row that merely refused to respond would leave the reader with
            // no answer and no idea why.
            <div className="space-y-3 text-sm text-muted">
              <p className="flex items-center gap-2">
                <FileText className="lucide-inline" aria-hidden="true" />
                {active?.in_progress
                  ? i18nT('pages.settings.releases.empty_in_progress')
                  : i18nT('pages.settings.releases.empty_released')}
              </p>
              <a
                className="inline-flex items-center gap-2 text-accent hover:underline"
                href={
                  prevStable
                    // An in-progress version has NO tag yet -- `compare/v0.1.2...v0.2.0`
                    // 404s while 0.2.0 is unreleased, which is the one row every
                    // prerelease reader lands on. Compare against the branch instead.
                    // The base is the newest release WITH notes, so a release that
                    // shipped without a section widens the range; that is a known
                    // over-report, and the alternative (guessing tags) needs the
                    // network this page deliberately does not use.
                    ? `${REPO}/compare/v${prevStable.version}...${active?.in_progress ? 'main' : `v${active?.version}`}`
                    : `${REPO}/commits`
                }
                target="_blank"
                rel="noreferrer noopener"
              >
                <GitCommitHorizontal className="lucide-inline" aria-hidden="true" />
                {i18nT('pages.settings.releases.view_commits')}
              </a>
            </div>
          )}
        </article>
      </div>
    </div>
  )
}
