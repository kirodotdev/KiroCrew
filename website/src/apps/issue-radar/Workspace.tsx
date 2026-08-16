// Issue Radar — three-column workspace shell.
//
//   ┌────────────┬─────────────┬──────────────────────────┐
//   │  LEFT RAIL  │ ISSUE LIST  │      ISSUE DETAIL        │
//   │ (accordion: │ (filtered   │  (metadata + body)       │
//   │  Dashboards │  by rail)   │                          │
//   │  / Filters) │             │                          │
//   └────────────┴─────────────┴──────────────────────────┘
//
// In 'dashboard' main view the list+detail split is replaced by a full-width
// dashboard page (Overview / Tagging), chosen from the
// registry. 'settings' shows the Settings page in the same area. 'crews' keeps
// the two-column shape with a different pair: the crew roster in the list column
// and the selected crew's page beside it — which is why it is a
// MainView and not a dashboard tab. The rail stays
// visible in every mode. All shared state comes from useIssueRadar(); this file
// owns only presentational layout (column resize).
import { useState } from 'react'
import { CircleDot, GitPullRequest, FilterX, Users } from 'lucide-react'
import { useIssueRadar } from './context'
import type { Crew } from './api'
import { Btn } from '../../components/ui'
import {
  loadListWidth, LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH, DEFAULT_LIST_WIDTH,
  loadRailWidth, loadRailCollapsed, RAIL_WIDTH_KEY, RAIL_COLLAPSED_KEY,
  MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, COLLAPSED_RAIL_WIDTH,
} from './lib/format'
import { loadColumnWidth } from '../../lib/columnWidth'
import { useColumnResize, type CollapseConfig } from '../../hooks/useColumnResize'
import LeftRail from './components/LeftRail'
import ResizeHandle from '../../components/ResizeHandle'
import IssueList from './components/IssueList'
import IssueDetail from './components/IssueDetail'
import PrList from './components/PrList'
import PrDetail from './components/PrDetail'
import CrewList from './components/CrewList'
import CrewEditor from './components/CrewEditor'
import CrewPageView from './views/CrewPageView'
import SettingsView from './views/SettingsView'
import { dashboardComponent } from './views/registry'
import { providerTerms } from './lib/links'

import { i18nT } from '../../i18n/t'
// Module-level so the hook's memoised resolver isn't invalidated every render.
const RAIL_COLLAPSE: CollapseConfig = { width: COLLAPSED_RAIL_WIDTH, storageKey: RAIL_COLLAPSED_KEY }

/** The crew roster column's own persisted width.
 *
 * Separate from `LIST_WIDTH_KEY` on purpose: the issue and PR lists share a key
 * because they hold the same shape of content and are never both on screen, while
 * the roster is a different column — its rows carry an avatar and a status line,
 * so a width that suits one is not the width that suits the other. Sharing the key
 * would also mean dragging one silently resized the other two. Bounds are reused
 * (240–600px), which is the range every list column in this app lives in. */
const CREW_LIST_WIDTH_KEY = 'kc:issue-radar:crew-list-width'
const loadCrewListWidth = () => loadColumnWidth(
  CREW_LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH, DEFAULT_LIST_WIDTH,
)

export default function Workspace() {
  const {
    mainView, dashboardTab, activeIssue, activePull, active,
    selectedIssue, anyFilterActive, clearFilters,
    selectedPull, anyPrFilterActive, clearPrFilters,
    crewView,
  } = useIssueRadar()
  // A selection resolved from the FILTERED list has no fallback (see context's
  // activeIssue/activePull), so an active filter that excludes the selected item
  // clears the detail pane. Without distinguishing that from "nothing selected",
  // the pane shows the same generic placeholder and — because the selection and
  // the filters are both persisted — stays blank across a tab switch or reload
  // with no hint that a filter is hiding it. These flags let the pane say so and
  // offer the one-click way out. The `selectedIssue != null` guard is what tells
  // "hidden by a filter" apart from a genuinely empty selection.
  const issueHiddenByFilter = !activeIssue && selectedIssue != null && anyFilterActive
  const pullHiddenByFilter = !activePull && selectedPull != null && anyPrFilterActive
  // Provider vocabulary: GitLab calls these merge requests, and calling them
  // pull requests in a GitLab workspace is simply wrong copy.
  const terms = providerTerms(active)
  const rail = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, RAIL_COLLAPSE, loadRailCollapsed,
  )
  const list = useColumnResize(LIST_WIDTH_KEY, loadListWidth, MIN_LIST_WIDTH, MAX_LIST_WIDTH)
  const crewList = useColumnResize(CREW_LIST_WIDTH_KEY, loadCrewListWidth, MIN_LIST_WIDTH, MAX_LIST_WIDTH)

  // The crew create/edit dialog's target. `null` = closed; `{crew: null}` = create;
  // `{crew}` = edit that record. A wrapper object, not a bare `Crew | null`, so
  // "closed" and "open on a new crew" are distinguishable — with one nullable
  // field they collapse into the same value and the dialog can never be closed
  // after a create. Transient by design: a restored-open dialog is not a page.
  const [crewEditor, setCrewEditor] = useState<{ crew: Crew | null } | null>(null)

  const DashboardView = dashboardComponent(dashboardTab)

  return (
    <div className="flex h-full bg-bg text-text">
      <LeftRail width={rail.width} collapsed={rail.collapsed} onExpand={rail.expand} />

      {/* Drag handle — resize the left rail. Present in every main view, since
          the rail itself is. Dragging well past the minimum collapses it. */}
      <ResizeHandle
        handleProps={rail.handleProps}
        label={i18nT('apps.issueRadar.workspace.resize_sidebar')}
        onNudge={rail.nudge}
        value={rail.width}
        min={MIN_RAIL_WIDTH}
        max={MAX_RAIL_WIDTH}
      />

      {mainView === 'issues' ? (
        <>
          <section style={{ width: list.width }} className="flex-shrink-0 min-h-0">
            <IssueList resizing={list.dragging} />
          </section>

          {/* Drag handle — resize the issue-list column. */}
          <ResizeHandle
            handleProps={list.handleProps}
            label={i18nT('apps.issueRadar.workspace.resize_list')}
            onNudge={list.nudge}
            value={list.width}
            min={MIN_LIST_WIDTH}
            max={MAX_LIST_WIDTH}
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activeIssue
              ? <IssueDetail issue={activeIssue} />
              : issueHiddenByFilter
                ? (
                  <HiddenByFilter
                    icon={<CircleDot size={26} strokeWidth={1.5} className="opacity-50" />}
                    message={i18nT('apps.issueRadar.workspace.selected_issue_hidden_by_filters')}
                    onClear={clearFilters}
                  />
                )
                : (
                  <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                    <CircleDot size={26} strokeWidth={1.5} className="opacity-50" />
                    <div className="text-[13px]">{i18nT('apps.issueRadar.workspace.select_an_issue_to_see_its_details')}</div>
                  </div>
                )}
          </main>
        </>
      ) : mainView === 'settings' ? (
        <main className="flex-1 min-w-0 min-h-0">
          <SettingsView />
        </main>
      ) : mainView === 'pulls' ? (
        <>
          <section style={{ width: list.width }} className="flex-shrink-0 min-h-0">
            <PrList resizing={list.dragging} />
          </section>

          {/* Drag handle — resize the PR-list column. */}
          <ResizeHandle
            handleProps={list.handleProps}
            label={i18nT('apps.issueRadar.workspace.resize_list')}
            onNudge={list.nudge}
            value={list.width}
            min={MIN_LIST_WIDTH}
            max={MAX_LIST_WIDTH}
          />

          <main className="flex-1 min-w-0 min-h-0">
            {activePull
              ? <PrDetail pull={activePull} />
              : pullHiddenByFilter
                ? (
                  <HiddenByFilter
                    icon={<GitPullRequest size={26} strokeWidth={1.5} className="opacity-50" />}
                    message={i18nT('apps.issueRadar.workspace.selected_change_hidden_by_filters', { subject: terms.changeRequestTitle })}
                    onClear={clearPrFilters}
                  />
                )
                : (
                  <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                    <GitPullRequest size={26} strokeWidth={1.5} className="opacity-50" />
                    <div className="text-[13px]">{i18nT('apps.issueRadar.workspace.select_a')} {terms.changeRequestTitle} {i18nT('apps.issueRadar.workspace.to_see_its_details')}</div>
                  </div>
                )}
          </main>
        </>
      ) : mainView === 'crews' ? (
        <>
          <section style={{ width: crewList.width }} className="flex-shrink-0 min-h-0">
            <CrewList onCreate={() => setCrewEditor({ crew: null })} />
          </section>

          {/* Drag handle — resize the crew-list column. Its own width key (see
              CREW_LIST_WIDTH_KEY): sharing LIST_WIDTH_KEY would make dragging the
              roster narrower also narrow the issue and PR lists, which are
              different columns holding different content. */}
          <ResizeHandle
            handleProps={crewList.handleProps}
            label={i18nT('apps.issueRadar.workspace.resize_list')}
            onNudge={crewList.nudge}
            value={crewList.width}
            min={MIN_LIST_WIDTH}
            max={MAX_LIST_WIDTH}
          />

          <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
            {crewView.kind === 'crew'
              ? <CrewPageView crewId={crewView.id} onEdit={(crew) => setCrewEditor({ crew })} />
              : (
                // Only reachable on a repo with no crews at all: context opens the
                // first crew as soon as the roster has one. Same placeholder shape
                // as the issue and PR panes, so an unaddressed main column reads
                // the same way everywhere in this app.
                <div className="h-full flex flex-col items-center justify-center text-muted gap-2">
                  <Users size={26} strokeWidth={1.5} className="opacity-50" />
                  {/* Assembled the same way the PR pane assembles its own
                      placeholder, so the two read identically and the noun comes
                      from the crews catalog rather than being spelled here. */}
                  <div className="text-[13px]">{i18nT('apps.issueRadar.workspace.select_a')} {i18nT('apps.issueRadar.views.crews.group_crew')} {i18nT('apps.issueRadar.workspace.to_see_its_details')}</div>
                </div>
              )}
          </main>

          {/* The create/edit dialog is mounted HERE rather than inside either
              column, because both raise it: the roster's "New Crew" creates, and
              the crew page's Edit opens the same form on a record. One owner also
              means one open dialog — two mounts would let a create and an edit
              sheet stack. Rendered only in this main view, so it cannot be opened
              from a page that has no crew context. */}
          <CrewEditor
            open={crewEditor !== null}
            onClose={() => setCrewEditor(null)}
            crew={crewEditor?.crew ?? null}
          />
        </>
      ) : (
        <main className="flex-1 min-w-0 overflow-y-auto scrollbar-none" style={{ scrollbarWidth: 'none' }}>
          <DashboardView />
        </main>
      )}
    </div>
  )
}

/** The detail-pane placeholder shown when the SELECTED item is filtered out of the
 * list (as opposed to nothing being selected). Names the cause and offers the
 * one-click exit, so a persisted selection hidden by a persisted filter is no
 * longer indistinguishable from an empty pane. */
function HiddenByFilter({
  icon, message, onClear,
}: {
  icon: React.ReactNode
  message: string
  onClear: () => void
}) {
  return (
    <div className="h-full flex flex-col items-center justify-center text-muted gap-3 px-6 text-center">
      {icon}
      <div className="text-[13px] max-w-xs">{message}</div>
      <Btn onClick={onClear}>
        <FilterX className="lucide-inline" />
        {i18nT('apps.issueRadar.workspace.clear_filters')}
      </Btn>
    </div>
  )
}
