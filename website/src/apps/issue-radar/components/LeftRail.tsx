import { LayoutDashboard, CircleDot, Settings, Radar, GitPullRequest, Users, ArrowUp, ArrowDown, ArrowUpDown, ListFilter, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import Clickable from '../../../components/Clickable'
import { fmtNumber } from '../../../i18n/format'
import { useIssueRadar } from '../context'
import type { RepoRef } from '../api'
import { APP_VERSION, CREW_SORT_FIELDS, DEFAULT_RAIL_WIDTH } from '../lib/format'
import { CREW_FILTERS, type CrewFilter } from '../lib/types'
import AccordionSection from './Accordion'
import { IconButton } from '../../../components/ui'
import DashboardsSection from './DashboardsSection'
import FiltersSection from './FiltersSection'
import PrFiltersSection from './PrFiltersSection'
import { ProviderLogo } from './ProviderBadge'
import SettingsSection from './SettingsSection'
import ReadOnlyTag, { isReadOnly } from './ReadOnlyTag'
import RepoSwitcher from './RepoSwitcher'
import { providerTerms } from '../lib/links'

import { i18nT } from '../../../i18n/t'
/** The left rail: a prominent repo switcher pinned at the top, then a
 * four-section accordion (Dashboards / Issues / Pull requests / Settings) that
 * follows the main view (see context follow-mode), with the app identity at the
 * very bottom. Clicking a section header navigates to that section's default
 * page (not just expand it), so you never stay on the previous view. The rail's
 * width is owned by Workspace, which renders the drag handle on its right edge;
 * the default is `w-72`. Dragging the handle far enough
 * past the minimum collapses the rail to `CollapsedRail`. */
export default function LeftRail({
  width = DEFAULT_RAIL_WIDTH, collapsed = false, onExpand, onNavigate, onCollapse,
  horizontal = false,
}: {
  width?: number | string
  /** Called after any section navigation. The narrow-viewport shell uses it to
   * collapse the full-width rail, so a tap does not navigate to a section that
   * the rail is still covering. */
  onNavigate?: () => void
  /** Set ONLY while narrow: renders an explicit collapse control, since the drag
   * handle that closes the rail on a desktop is hidden on touch. */
  onCollapse?: () => void
  collapsed?: boolean
  /** Only meaningful with `collapsed`: lay the strip across the top instead of
   * down the left edge, so the panes below keep the full viewport width. */
  horizontal?: boolean
  onExpand?: () => void
}) {
  const {
    expanded, dashboardTab, active, repos, openDashboard, openIssues, openPulls, openSettings,
    openCrews,
  } = useIssueRadar()
  // Provider vocabulary: GitLab calls these merge requests, and calling them
  // pull requests in a GitLab workspace is simply wrong copy.
  const terms = providerTerms(active)

  if (collapsed) {
    // Matched on the FULL identity, not just owner/repo: on a mixed install the
    // same slug can exist on two providers, and a loose match would badge the
    // collapsed rail with the other repo's write access.
    const activeEntry = repos.find(
      (r) => r.owner === active.owner
        && r.repo === active.repo
        && (r.provider || 'github') === (active.provider || 'github')
        && (r.host || 'github.com') === (active.host || 'github.com'),
    )
    return (
      <CollapsedRail
        width={width}
        owner={active.owner}
        repo={active.repo}
        // The mark has to say which provider this repo is on for the same reason
        // the open rail's switcher does: `owner/repo` alone is identical across
        // providers, so a hard-coded GitHub logo mislabels every other one.
        repoRef={active}
        readOnly={isReadOnly(activeEntry?.permissions)}
        onExpand={onExpand}
        horizontal={horizontal}
      />
    )
  }

  return (
    <aside style={{ width }} className="flex-shrink-0 flex flex-col min-h-0 py-2 gap-2">
      {/* A narrow viewport gives the expanded rail the WHOLE screen, and its drag
          handle is hidden on touch, so without this a user who opened the rail to
          look at it can only leave by navigating somewhere. */}
      {onCollapse && (
        <div className="px-2 flex justify-end">
          <IconButton aria-label={i18nT('app.collapse_sidebar')} onClick={onCollapse}>
            <PanelLeftClose size={16} />
          </IconButton>
        </div>
      )}
      {/* Repo switcher — top of the rail, opens downward. */}
      <div className="px-2">
        <RepoSwitcher />
      </div>

      <AccordionSection
        title={i18nT('apps.issueRadar.components.leftRail.dashboards')}
        icon={LayoutDashboard}
        expanded={expanded === 'dashboards'}
        // Return to the dashboard you were last on, not Overview: `dashboardTab`
        // is already persisted, so resetting it here would throw away the one
        // piece of state the section is meant to remember.
        onToggle={() => { openDashboard(dashboardTab); onNavigate?.() }}
      >
        <DashboardsSection onNavigate={onNavigate} />
      </AccordionSection>

      <AccordionSection
        title={i18nT('apps.issueRadar.views.crews.rail_section')}
        icon={Users}
        expanded={expanded === 'crews'}
        // Return to the crews page you were last on — `crewView` is persisted, so
        // resetting it here would discard the one thing the section remembers.
        // Same contract as Dashboards above.
        onToggle={() => { openCrews(); onNavigate?.() }}
      >
        <CrewsSection onNavigate={onNavigate} />
      </AccordionSection>

      <AccordionSection
        // The rail's own noun for the tracked item, which is not "Issues"
        // everywhere: Azure DevOps tracks work items, and "Issue" is only one of
        // the types some of its process templates even define.
        title={i18nT(terms.trackedItemPluralTitleKey)}
        icon={CircleDot}
        expanded={expanded === 'filters'}
        onToggle={() => { openIssues(); onNavigate?.() }}
      >
        <FiltersSection onNavigate={onNavigate} />
      </AccordionSection>

      <AccordionSection
        title={terms.changeRequestPluralTitle}
        icon={GitPullRequest}
        expanded={expanded === 'pulls'}
        onToggle={() => { openPulls(); onNavigate?.() }}
      >
        <PrFiltersSection onNavigate={onNavigate} />
      </AccordionSection>

      <AccordionSection
        title={i18nT('apps.issueRadar.components.leftRail.settings')}
        icon={Settings}
        expanded={expanded === 'settings'}
        onToggle={() => { openSettings(); onNavigate?.() }}
      >
        <SettingsSection onNavigate={onNavigate} />
      </AccordionSection>

      {/* App identity — bottom-most. */}
      <div className="px-3 pb-2 flex items-center gap-2">
        <Radar size={16} className="text-accent flex-shrink-0" />
        <span className="text-[14px] font-medium text-text">{i18nT('apps.issueRadar.components.leftRail.issue_radar')}</span>
        <span className="ml-auto text-[12px] text-muted opacity-70">{i18nT('apps.issueRadar.components.leftRail.v')}{APP_VERSION}</span>
      </div>
    </aside>
  )
}

/** Body of the "Crews" accordion section: Sort and Filters over the roster — the
 * crew analogue of FiltersSection / PrFiltersSection, and the reason those
 * controls are NOT in column 2: the rail holds how you narrow a list, the list
 * column holds the list.
 *
 * The roster itself is deliberately absent here. Column 2 is where a crew's state
 * is read, and the rail is the surface that has to stay legible at 220px — a
 * second copy of one list, without the state, is the worse of the two. */
function CrewsSection({ onNavigate }: { onNavigate?: () => void }) {
  const {
    openCrews, crewCounts,
    crewFilter, setCrewFilter, crewSortKey, crewSortDir, cycleCrewSort,
  } = useIssueRadar()

  const rowClass = (isActive: boolean) =>
    `w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left cursor-pointer transition-colors ${
      isActive ? 'bg-accent-subtle text-text font-medium' : 'text-muted hover:bg-bg-hover'
    }`


  /** Filter labels and tallies keyed by filter, so the rows are driven by
   * `CREW_FILTERS` itself — a filter added to the list without a label or a count
   * fails to compile here.
   *
   * The counts are the SERVER's, summed from each crew's open work items: data the
   * roster payload does not carry, so they cannot be derived client-side. They are
   * independent predicates, not a partition, and are allowed to sum past the roster
   * size — a paused crew holding in-flight work is counted in both `working` and
   * `paused`. */
  const FILTER_LABEL: Record<CrewFilter, string> = {
    all: i18nT('apps.issueRadar.views.crews.filter_all'),
    working: i18nT('apps.issueRadar.views.crews.filter_working'),
    paused: i18nT('apps.issueRadar.views.crews.filter_paused'),
  }
  const FILTER_COUNT: Record<CrewFilter, number> = {
    all: crewCounts.on_duty,
    working: crewCounts.working,
    paused: crewCounts.paused,
  }

  return (
    <div className="px-3 pt-1">
      {/* Sort and filters only — no destinations. This surface's destinations
          already live in column 2, where each crew is its own card. Repeating them
          here is a second copy of one list, and the copy without the state. The
          issues and PR sections set the shape: the rail narrows a list, the list
          column holds it. */}
      <div className="pt-1">
        <div className="flex items-center gap-1.5 mb-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
          <ArrowUpDown size={12} /> {i18nT('apps.issueRadar.views.crews.rail_sort')}
        </div>
        <div className="flex flex-col gap-0.5">
          {CREW_SORT_FIELDS.map((f) => {
            const isActive = f.key === crewSortKey
            const DirIcon = crewSortDir === 'asc' ? ArrowUp : ArrowDown
            return (
              <Clickable
                key={f.key}
                onClick={() => cycleCrewSort(f.key)}
                data-testid={`crew-sort-${f.key}`}
                aria-pressed={isActive}
                className={rowClass(isActive)}
              >
                <f.icon size={14} className="flex-shrink-0" />
                <span className="flex-1">{f.label}</span>
                {isActive && <DirIcon size={14} className="text-accent" />}
              </Clickable>
            )
          })}
        </div>
      </div>

      <div className="pt-5">
        <div className="flex items-center gap-1.5 mb-1.5 text-[12px] font-semibold text-muted uppercase tracking-[.05em]">
          <ListFilter size={12} /> {i18nT('apps.issueRadar.views.crews.rail_filters')}
        </div>
        <div className="flex flex-col gap-0.5">
          {/* Mutually exclusive, like the PR lifecycle rows: picking one replaces
              the last. Each carries the server's tally on the right, which is why
              these are not plain FilterRows. */}
          {CREW_FILTERS.map((key) => {
            const isActive = crewFilter === key
            return (
              <Clickable
                key={key}
                onClick={() => { setCrewFilter(key); openCrews(); onNavigate?.() }}
                data-testid={`crew-filter-${key}`}
                aria-pressed={isActive}
                className={rowClass(isActive)}
              >
                <span className="flex-1">{FILTER_LABEL[key]}</span>
                <span className="flex-shrink-0 text-[12px] text-muted opacity-80">
                  {fmtNumber(FILTER_COUNT[key])}
                </span>
              </Clickable>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/** The rail dragged shut: one rounded-rect card carrying the provider logo and
 * the full `owner/repo`, with the app mark alongside — the same elevated-pill
 * treatment the repo switcher gets when the rail is open, so you still know
 * which repo the workspace points at while the list and detail columns take the
 * window. Clicking the repo half reopens the rail at its previous width
 * (dragging the handle back out works too).
 *
 * Two orientations, and the axis is the whole point. Down the left edge on a
 * desktop, where a strip's width is free. Across the TOP while narrow, where it
 * is not: a phone can spend vertical room and has none to the side. The vertical
 * card rotates the repo label clockwise (top-to-bottom reading) so it starts
 * next to the logo; the bar just truncates it. */
function CollapsedRail({
  width, owner, repo, repoRef, readOnly, onExpand, horizontal = false,
}: {
  // Shares LeftRail's pass-through prop, so it takes the same CSS-length type.
  // In practice this is always the numeric strip width: the string form is only
  // used for the full-width narrow-viewport rail, which is the EXPANDED branch.
  width: number | string
  owner: string
  repo: string
  /** The active repo's identity — only its provider is read, to pick the mark. */
  repoRef?: Pick<RepoRef, 'provider'>
  readOnly: boolean
  onExpand?: () => void
  /** Lay the collapsed rail across the TOP instead of down the left edge. Set
   * while narrow, where the strip's ~48px is horizontal space the reading
   * column cannot spare: a phone has vertical room to give and none to the
   * side, and CJK body text pays for a squeezed column by the character. */
  horizontal?: boolean
}) {
  const full = `${owner}/${repo}`
  if (horizontal) {
    return (
      <aside className="w-full flex-shrink-0 px-4 pt-2">
        <div className="flex items-center overflow-hidden rounded-xl border border-border-strong bg-bg-elevated shadow-sm">
          <button
            type="button"
            onClick={onExpand}
            title={i18nT('apps.issueRadar.components.leftRail.click_to_expand_the_sidebar', { name: full })}
            aria-label={i18nT('apps.issueRadar.components.leftRail.expand_sidebar')}
            className="flex-1 min-w-0 flex items-center gap-2 px-3 py-2 cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors focus-ring"
          >
            <ProviderLogo repoRef={repoRef} size={16} className="flex-shrink-0 text-text" />
            {/* Truncates from the tail: the repo half of `owner/repo` is what
                tells two workspaces apart, and it survives longest that way. */}
            <span className="min-w-0 truncate text-[13px] font-medium tracking-[.02em] text-text">
              {full}
            </span>
            {readOnly && <ReadOnlyTag repoRef={repoRef} />}
            {/* Opens toward the reader, matching the expanded rail's
                PanelLeftClose — the bar is horizontal but the panel it reveals
                is still the left rail. */}
            <PanelLeftOpen size={15} className="ml-auto flex-shrink-0" />
          </button>
          {/* Adornment, not a control: no divider and no hover, because a
              bordered cell beside a real button reads as a second button. */}
          <div
            className="flex-shrink-0 flex items-center pl-1 pr-3"
            title={i18nT('apps.issueRadar.components.leftRail.issue_radar_version', { version: APP_VERSION })}
          >
            <Radar size={15} className="text-accent" />
          </div>
        </div>
      </aside>
    )
  }
  return (
    <aside style={{ width }} className="flex-shrink-0 flex flex-col min-h-0 py-2 px-1">
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden rounded-xl border border-border-strong bg-bg-elevated shadow-sm">
        <button
          type="button"
          onClick={onExpand}
          title={i18nT('apps.issueRadar.components.leftRail.click_to_expand_the_sidebar', { name: full })}
          aria-label={i18nT('apps.issueRadar.components.leftRail.expand_sidebar')}
          className="flex-1 min-h-0 w-full flex flex-col items-center gap-3 pt-3.5 pb-2 cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors focus-ring"
        >
          <ProviderLogo repoRef={repoRef} size={18} className="flex-shrink-0 text-text" />
          <div className="min-h-0 flex flex-col items-center gap-2">
            {/* Rotated CLOCKWISE (writing-mode alone, no counter-rotation) so the
                string reads top-to-bottom, starting right under the logo — the
                same icon-then-label order as the open rail, just turned. It
                truncates at the strip's height instead of pushing the read-only
                flag out of view. */}
            <span
              className="min-h-0 max-w-full overflow-hidden text-ellipsis whitespace-nowrap text-[13px] font-medium tracking-[.02em]"
              style={{ writingMode: 'vertical-rl' }}
            >
              {full}
            </span>
            {/* Write access constrains what the workspace can do, so the flag
                survives the collapse — trailing the repo name exactly as it does
                in the open rail's switcher, just turned with it. */}
            {readOnly && <ReadOnlyTag vertical repoRef={repoRef} />}
          </div>
        </button>
        {/* App mark only — the name doesn't earn its space at 48px, so it and
            the version live in the title. */}
        <div
          className="flex-shrink-0 flex justify-center pb-3.5"
          title={i18nT('apps.issueRadar.components.leftRail.issue_radar_version', { version: APP_VERSION })}
        >
          <Radar size={16} className="text-accent" />
        </div>
      </div>
    </aside>
  )
}
