/**
 * The release notes section at the foot of Settings → About: the running
 * build's own release, then a capped list of earlier ones.
 *
 * ## Why it lives inside About, and why the list is capped
 *
 * About states the identity of *this install* — version, licence, platform,
 * channel, update controls — and that content is bounded: one screen today and
 * one screen in five years. A changelog grows without limit, and when it was
 * once inlined in About, open by default, the page became an ever-growing
 * scroll. It then moved to a Releases tab of its own, which kept About short
 * but split one question ("what am I on, can I update, what changes?") across
 * two tabs.
 *
 * The section keeps About bounded instead by what it shows unasked: the
 * running release's summary (the rest behind "Read all notes"), and FIVE
 * earlier releases as collapsed one-line rows. A row opens in place; "Show N
 * more" adds five rows at a time, only when asked. `RELEASE_PARAM`
 * (`/settings/about?release=<version>`, where the old
 * `/settings/releases/<version>` links now land) opens and scrolls to one
 * release; `?release=notes`, where the bare `/settings/releases` lands, scrolls
 * to and focuses the section.
 *
 * ## Why the list is short
 *
 * Rows come from {@link https://github.com/kirodotdev/KiroCrew | CHANGELOG.md}'s
 * sections plus the release the running build belongs to — nothing else. A
 * version that shipped without a section is deliberately absent, because a row
 * that cannot say anything is indistinguishable from a broken one. The running
 * build's release is the single exception: "what am I on?" is the question this
 * section answers first. `kiro_crew/changelog.py` owns that rule.
 *
 * ## Why prereleases have no rows of their own
 *
 * `0.2.0-rc.1` and `0.2.0-nightly.<stamp>` are drafts of 0.2.0, not releases.
 * Both collapse onto the single `0.2.0` release, flagged in-progress, so one
 * mechanism serves both channels, nightly does not add ~4 rows a day forever,
 * and the list does not move when 0.2.0 is promoted — the release is already
 * there and merely loses the flag. Which RC you are on is stated under its
 * heading.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ArrowUp, ChevronRight, FileText, GitCommitHorizontal, History, Loader2, Sparkles } from 'lucide-react'
import { api } from '../../api/client'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { Card, CardTitle, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
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

/** Earlier releases shown before "Show more", and how many each click adds. */
export const EARLIER_PAGE = 5

/** The About query parameter that opens one release: `?release=0.12.0`. */
export const RELEASE_PARAM = 'release'

/** `?release=notes`: no single release, just the release notes section (the old bare `/settings/releases`). */
export const RELEASE_NOTES_LINK = 'notes'

/**
 * A release body's opening summary: the paragraphs before its first heading,
 * bullet or bold-led paragraph. The rest of the body stays behind "Read all
 * notes", so a long release does not push the earlier ones off the page.
 */
export function releaseSummary(body: string): { summary: string; rest: string } {
  const blocks = body.trim().split(/\n\s*\n/)
  const lead: string[] = []
  for (const block of blocks) {
    if (/^\s*(#|[-*+]\s|\*\*|\d+\.\s|```|\|)/.test(block)) break
    lead.push(block)
  }
  return { summary: lead.join('\n\n'), rest: blocks.slice(lead.length).join('\n\n') }
}

/** A linked version as a release: `v0.12.0-rc.1` → `0.12.0`. */
function linkedRelease(raw: string): string {
  return raw.trim().replace(/^v/i, '').split(/[-+]/)[0]
}

/** Small-print date for a row, or the reason it has none. */
function rowNote(r: Release): string {
  return r.date || i18nT('pages.settings.releases.row_no_notes')
}

/**
 * The commit range behind a release: from the newest RELEASED version older
 * than it. An in-progress version has NO tag yet -- `compare/v0.1.2...v0.2.0`
 * 404s while 0.2.0 is unreleased -- so it is compared against the branch. A
 * release that shipped without a section widens the range; that is a known
 * over-report, and the alternative (guessing tags) needs the network this
 * section deliberately does not use.
 */
function commitsUrl(release: Release, older: Release | undefined): string {
  if (!older) return `${REPO}/commits`
  return `${REPO}/compare/v${older.version}...${release.in_progress ? 'main' : `v${release.version}`}`
}

function CommitsLink({ href }: { href: string }) {
  return (
    <a
      className="inline-flex items-center gap-2 text-sm text-accent hover:underline"
      href={href}
      target="_blank"
      rel="noreferrer noopener"
    >
      <GitCommitHorizontal className="lucide-inline" aria-hidden="true" />
      {i18nT('pages.settings.releases.view_commits')}
    </a>
  )
}

/** A release's notes, or why it has none and where its changes are. */
function ReleaseNotes({ release, older, alwaysLinkCommits, summarize }: {
  release: Release
  older: Release | undefined
  alwaysLinkCommits?: boolean
  /** Show the opening summary and fold the rest into "Read all notes". */
  summarize?: boolean
}) {
  const href = commitsUrl(release, older)
  if (release.body) {
    const { summary, rest } = summarize ? releaseSummary(release.body) : { summary: release.body, rest: '' }
    return (
      <div className="flex flex-col gap-3">
        {summary && <MarkdownRenderer content={summary} />}
        {rest && (
          <details className="text-sm" data-testid="release-read-all">
            <summary className="cursor-pointer text-accent hover:underline w-fit">
              {i18nT('pages.settings.releases.read_all_notes')}
            </summary>
            <div className="mt-3">
              <MarkdownRenderer content={rest} />
            </div>
          </details>
        )}
        {alwaysLinkCommits && <CommitsLink href={href} />}
      </div>
    )
  }
  // No section for this version. It still opens, precisely so the absence can
  // be explained here and a real path offered: a row that refused to respond
  // would leave the reader with no answer and no idea why.
  return (
    <div className="space-y-3 text-sm text-muted">
      <p className="flex items-center gap-2">
        <FileText className="lucide-inline" aria-hidden="true" />
        {release.in_progress
          ? i18nT('pages.settings.releases.empty_in_progress')
          : i18nT('pages.settings.releases.empty_released')}
      </p>
      <CommitsLink href={href} />
    </div>
  )
}

export default function ReleasesPanel() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['releases'],
    queryFn: () => api.releases() as Promise<ReleasesPayload>,
  })
  const [params] = useSearchParams()
  const linked = params.get(RELEASE_PARAM) || ''

  // Memoised so the `?? []` fallback does not mint a new array identity on
  // every render and re-fire the link effect below.
  const releases = useMemo(() => data?.releases ?? [], [data?.releases])
  const current = releases.find(r => r.is_current)
  // The list is newest first. Rows above the running release are newer than
  // this build (a changelog that got ahead of the version stamp), so they go in
  // their own group rather than under "Earlier releases".
  const currentIndex = current ? releases.indexOf(current) : -1
  const newer = useMemo(() => (currentIndex > 0 ? releases.slice(0, currentIndex) : []), [releases, currentIndex])
  const earlier = useMemo(() => releases.slice(currentIndex + 1), [releases, currentIndex])
  const [shown, setShown] = useState(EARLIER_PAGE)
  const [open, setOpen] = useState('')
  // The link this section last acted on, and what it found: `''` none, a
  // version, `current`, or `missing`.
  const handledLink = useRef('')
  const [linkTarget, setLinkTarget] = useState('')
  // Index of the row "Show more" revealed first, to move focus to it once drawn.
  const [focusRow, setFocusRow] = useState(-1)
  const sectionRef = useRef<HTMLDivElement>(null)
  const currentRef = useRef<HTMLDivElement>(null)
  const earlierRef = useRef<HTMLDivElement>(null)

  // `/settings/about?release=<version>`: open that release and bring it into
  // view, once per link, after the list has loaded.
  useEffect(() => {
    if (!linked) {
      setLinkTarget('')
      handledLink.current = ''
      return
    }
    if (releases.length === 0 || handledLink.current === linked) return
    handledLink.current = linked
    if (linked === RELEASE_NOTES_LINK) {
      setLinkTarget('section')
      return
    }
    const want = linkedRelease(linked)
    if (current?.version === want) {
      setLinkTarget('current')
      return
    }
    if (newer.some(r => r.version === want)) {
      setOpen(want)
      setLinkTarget(want)
      return
    }
    const index = earlier.findIndex(r => r.version === want)
    if (index < 0) {
      setLinkTarget('missing')
      return
    }
    setOpen(want)
    setShown(s => Math.max(s, Math.ceil((index + 1) / EARLIER_PAGE) * EARLIER_PAGE))
    setLinkTarget(want)
  }, [linked, releases, current, newer, earlier])

  useEffect(() => {
    if (!linkTarget) return
    if (linkTarget === 'section') {
      // The old Releases tab's address: bring the section into view and put
      // focus on it, so a keyboard or screen reader user starts there too.
      sectionRef.current?.scrollIntoView?.({ block: 'start' })
      sectionRef.current?.focus({ preventScroll: true })
      return
    }
    const el = linkTarget === 'current'
      ? currentRef.current
      : linkTarget === 'missing'
        ? earlierRef.current ?? currentRef.current
        : document.getElementById(`release-${linkTarget}`)
    // Optional call: jsdom has no scrollIntoView.
    el?.scrollIntoView?.({ block: 'start' })
  }, [linkTarget])

  useEffect(() => {
    if (focusRow < 0) return
    earlierRef.current?.querySelectorAll<HTMLButtonElement>('li > button[aria-expanded]')[focusRow]?.focus()
    setFocusRow(-1)
  }, [focusRow])

  if (isLoading) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
          {i18nT('pages.settings.releases.loading')}
        </div>
      </Card>
    )
  }
  if (releases.length === 0) {
    // A fetch failure and an archive with nothing in it are different answers,
    // and saying "not available in this build" for a 500 sends the reader to
    // look for a build problem that does not exist. `isError` is only reached
    // with no cached entries: a failed REFETCH keeps `data`, and the archive is
    // a static document, so stale-but-present beats both messages.
    return (
      <Card>
        {isError
          // Nothing to lose: the archive is read-only, so the hand-off is on.
          ? <ErrorNotice message={i18nT('pages.settings.releases.load_failed')} askAgent />
          : <p className="text-sm text-muted">{i18nT('pages.settings.releases.unavailable')}</p>}
      </Card>
    )
  }

  /** The newest RELEASED version older than `r` (the list is newest first). */
  const olderReleased = (r: Release) => {
    const index = releases.indexOf(r)
    return releases.slice(index + 1).find(x => !x.in_progress)
  }
  // Shown in the earlier-releases card, or the running release's when that is
  // the only one, so a link to an unknown version always says why nothing opened.
  const missingNotice = linkTarget === 'missing' && (
    <p className="mb-3 rounded-lg bg-bg-accent px-3 py-2 text-xs text-muted" data-testid="releases-link-missing">
      {i18nT('pages.settings.releases.link_not_found', { version: linkedRelease(linked) })}
    </p>
  )
  const visible = earlier.slice(0, shown)
  const remaining = earlier.length - visible.length

  const renderRow = (r: Release) => {
    const isOpen = open === r.version
    const panelId = `release-notes-${r.version}`
    return (
      <li key={r.version} id={`release-${r.version}`} className="scroll-mt-4">
        <button
          type="button"
          aria-expanded={isOpen}
          // Only while open: a closed row's notes are not mounted.
          aria-controls={isOpen ? panelId : undefined}
          onClick={() => setOpen(isOpen ? '' : r.version)}
          className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-bg-hover cursor-pointer bg-transparent border-none"
        >
          <ChevronRight
            size={14}
            className={`lucide-inline shrink-0 text-muted transition-transform motion-reduce:transition-none ${isOpen ? 'rotate-90' : ''}`}
            aria-hidden="true"
          />
          <span className="text-sm font-semibold tabular-nums text-text">{r.version}</span>
          <span className="ml-auto text-xs text-muted tabular-nums">{rowNote(r)}</span>
        </button>
        {isOpen && (
          <div id={panelId} className="px-2 pb-4 pt-1 sm:pl-8">
            <ReleaseNotes release={r} older={olderReleased(r)} />
          </div>
        )}
      </li>
    )
  }

  return (
    // The release notes section as one landmark, so the old Releases address
    // (`?release=notes`) has one place to scroll to and focus.
    <div
      ref={sectionRef}
      id="release-notes"
      tabIndex={-1}
      role="region"
      aria-label={i18nT('pages.settings.releases.section_label')}
      data-setting-label={i18nT('pages.settings.releases.section_label')}
      className="scroll-mt-4 outline-hidden"
      data-testid="release-notes-section"
    >
      {newer.length > 0 && (
        <Card>
          <div data-testid="releases-newer">
            <CardTitle className="flex-wrap">
              <ArrowUp size={15} className="lucide-inline" aria-hidden="true" />
              {i18nT('pages.settings.releases.newer_title')}
            </CardTitle>
            <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('pages.settings.releases.newer_title')}>
              {newer.map(renderRow)}
            </ul>
          </div>
        </Card>
      )}

      {current && (
        <Card>
          <div ref={currentRef} className="scroll-mt-4" data-testid="releases-current">
            <CardTitle className="mb-1 flex-wrap">
              <Sparkles size={15} className="lucide-inline" aria-hidden="true" />
              {i18nT('pages.settings.releases.whats_new_in', { version: current.version })}
              {/* Which row is the reader's own, so counting back from it works. */}
              <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-medium text-muted" data-testid="releases-installed">
                {i18nT('pages.settings.releases.badge_installed')}
              </span>
              {current.in_progress && (
                // OUTLINE, not a solid accent fill: solid `bg-accent` with
                // `text-accent-fg` at this size is this codebase's
                // notification-COUNT idiom, and a passive status label must
                // not read as a count of something waiting.
                <span className="rounded border border-accent/40 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                  {i18nT('pages.settings.releases.badge_unreleased')}
                </span>
              )}
            </CardTitle>
            <p className="mb-3 text-xs text-muted tabular-nums">
              {current.in_progress
                ? i18nT('pages.settings.releases.detail_on_prerelease', { version: data!.current_version })
                : current.date || i18nT('pages.settings.releases.detail_no_date')}
            </p>
            {earlier.length === 0 && missingNotice}
            {/* The archive is read from this install, never the network, so a
                prerelease build shows it as of its branch point. Said out loud
                instead of letting the list look complete when it is not. */}
            {data?.stale && (
              <p className="mb-3 rounded-lg bg-bg-accent px-3 py-2 text-xs text-muted">
                {i18nT('pages.settings.releases.stale_note', { version: data.current_version })}
              </p>
            )}
            <ReleaseNotes release={current} older={olderReleased(current)} alwaysLinkCommits summarize />
          </div>
        </Card>
      )}

      {earlier.length > 0 && (
        <Card>
          <div ref={earlierRef} className="scroll-mt-4" data-testid="releases-earlier">
            <CardTitle className="flex-wrap">
              <History size={15} className="lucide-inline" aria-hidden="true" />
              {current ? i18nT('pages.settings.releases.earlier_title') : i18nT('pages.settings.releases.releases_title')}
              <span className="text-xs font-normal text-muted tabular-nums">
                {i18nT('pages.settings.releases.earlier_count', { count: earlier.length })}
              </span>
            </CardTitle>
            {missingNotice}
            <ul className="flex flex-col divide-y divide-border" aria-label={i18nT('pages.settings.releases.list_label')}>
              {visible.map(renderRow)}
            </ul>
            {remaining > 0 && (
              <div className="mt-2">
                <Btn
                  onClick={() => {
                    // The button unmounts on the last page, so focus moves to
                    // the first row it revealed instead of falling to <body>.
                    setFocusRow(shown)
                    setShown(s => s + EARLIER_PAGE)
                  }}
                  data-testid="releases-show-more"
                >
                  {i18nT('pages.settings.releases.show_more', { count: Math.min(EARLIER_PAGE, remaining) })}
                </Btn>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  )
}
