// The shell: rail | detail. Two columns, not three.
//
// The lists (pull requests / reviews) and the repo picker are stacked in the
// rail; everything to the right of it is the report you are reading. A third
// column spent a fixed slice of the window on a list you had already used to get
// where you are — and when no repo was selected it held nothing but an empty
// state pointing back at the rail.
//
// Reports are the widest content in the app (finding bodies, diffs, check
// tables), so the space belongs to them.
import { ScanSearch } from 'lucide-react'
import { Suspense, lazy, useEffect } from 'react'

import { IconButton } from '../../components/ui'
import ListDetailBack from '../../components/ListDetailBack'
import { type CollapseConfig, useColumnResize } from '../../hooks/useColumnResize'
import { useIsMobile } from '../../hooks/useIsMobile'
import EmptyState from './components/EmptyState'
import LeftRail from './components/LeftRail'
import PrReviewDetail from './components/PrReviewDetail'
import RailHeader, { sectionHasList, sectionLabel } from './components/RailHeader'
import RunDetail from './components/RunDetail'
import { useSage } from './context'
import {
  COLLAPSED_RAIL_WIDTH, MAX_RAIL_WIDTH, MIN_RAIL_WIDTH, RAIL_COLLAPSED_KEY, RAIL_WIDTH_KEY,
  loadRailCollapsed, loadRailWidth,
} from './lib/layout'
import LearningView from './views/LearningView'
import SettingsView from './views/SettingsView'
import type { ListTab, MainView } from './lib/types'

// Lazy: the local-review surface (diff rendering, finding forms, review-fix
// setup) is the app's heaviest subtree and is only mounted when the operator
// opens the Local tab, so keeping it out of the eager chunk keeps every other
// entry's initial download smaller.
const LocalReviewView = lazy(() => import('./views/LocalReviewView'))

import { i18nT } from '../../i18n/t'
/** The 6px vertical drag handle between two columns. */
function Splitter({ handleProps, label }: {
  handleProps: ReturnType<typeof useColumnResize>['handleProps']
  label: string
}) {
  return (
    <div
      {...handleProps}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={i18nT('apps.codeReviewSage.workspace.drag_to_resize')}
      className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-accent/30 transition-colors"
      style={{ touchAction: 'none' }}
    />
  )
}

// Module-level so the resize hook's memoised resolver isn't invalidated every
// render. `whenNarrow` collapses the rail to its strip on a phone, where a 280px
// minimum would otherwise leave the report pane ~100px of a 390px viewport. The
// opened state below is FULL WIDTH, not `rail.width`: a strip whose expand button
// only restored 280px would hand the user straight back into the squeeze.
const RAIL_COLLAPSE: CollapseConfig = {
  width: COLLAPSED_RAIL_WIDTH, storageKey: RAIL_COLLAPSED_KEY, whenNarrow: true,
}

/** The name of the pane the rail will show for `view`.
 *
 * Deliberately NOT the section's own name. A control reading "Reviews" that opens
 * the Pull requests tab, or "Learning" that opens a panel headed "Namespaces",
 * names something the user cannot see — the defect class this shell exists to
 * remove, so it must not reappear in the control pointing AT the rail. Each
 * section therefore answers with its rail's own heading: reviews from the active
 * list tab, learning from the namespaces panel. A section whose rail has no pane
 * of its own falls back to the section name (and `sectionHasList` withholds the
 * control there anyway).
 *
 * A function rather than a field on RailHeader's section table, because the
 * reviews answer depends on runtime state (`listTab`) and a static key cannot
 * express it. Resolved per call so a language switch reaches it.
 */
function railPaneLabel(view: MainView, listTab: ListTab): string {
  if (view === 'reviews') {
    return i18nT(listTab === 'reviews'
      ? 'apps.codeReviewSage.components.middleColumn.reviews'
      : 'apps.codeReviewSage.components.middleColumn.pull_requests')
  }
  if (view === 'learning') {
    return i18nT('apps.codeReviewSage.components.learningRail.namespaces')
  }
  return sectionLabel(view)
}

export default function Workspace() {
  const { mainView, activeRun, selectedPr, listTab } = useSage()

  const rail = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, RAIL_COLLAPSE, loadRailCollapsed,
  )
  const isMobile = useIsMobile()
  // Narrow AND expanded: the rail IS the page, so the report steps aside below.
  const mobileRailOpen = isMobile && !rail.collapsed
  // Narrow AND collapsed: the strip lies across the TOP instead of down the left
  // edge. Its ~44px is free on a desktop and a tenth of the reading column on a
  // phone, and horizontal is the axis a phone has none of to spare — CJK body
  // text pays for a squeezed column by the character, because it breaks almost
  // anywhere and collapses rather than overflowing.
  const railBar = isMobile && rail.collapsed
  // Collapse on select — the third leg of this pattern, and the one that makes
  // the expanded rail a drill-down instead of a one-way door. Without it, picking
  // a review from the full-width rail changes nothing visible: the rail keeps the
  // viewport, the report stays hidden, and the drag handle that would otherwise
  // collapse it is gone on touch. `whenNarrow`'s own contract requires it.
  //
  // Keyed on the selection rather than run imperatively from the row handler,
  // because selection lives in the Sage context, which cannot reach this hook.
  // Keying on identity is what keeps it from fighting the user's own expand: it
  // fires when the selection actually changes, not on every mobile render.
  useEffect(() => {
    if (isMobile) rail.collapse()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPr, activeRun, mainView, isMobile])
  // Every main pane shares this: hidden while the rail owns the viewport, so a
  // 100%-wide rail cannot push the report off-screen instead of replacing it.
  const mainClass = `flex-1 min-w-0 min-h-0 flex-col ${mobileRailOpen ? 'hidden' : 'flex'}`
  // Whether reopening the rail leads anywhere from here. Settings' rail body is
  // an empty spacer (see LeftRail), so expanding it there hands the user a
  // viewport-filling panel holding nothing but this header — which is exactly
  // what the back control already refuses to promise. ONE predicate for BOTH
  // routes into the rail: gating only the labelled one let the icon re-tap open
  // the empty panel the label was withheld to avoid.
  const canOpenList = railBar && sectionHasList(mainView)
  // Name the pane the control actually LANDS ON, not the section it belongs to —
  // see railPaneLabel.
  const listLabel = railPaneLabel(mainView, listTab)
  // The bar's labelled route back to the list, which on this shell means
  // reopening the rail — that is where both lists live.
  const backToList = canOpenList
    ? <ListDetailBack label={listLabel} onBack={rail.expand} />
    : undefined

  return (
    // overflow-hidden so a mis-sized child can never grow the shell past the
    // viewport and push the rail's identity footer below the fold — each column
    // owns its own scrolling.
    <div className={`flex h-full overflow-hidden bg-bg text-text ${railBar ? 'flex-col' : ''}`}>
      <div
        style={{ width: railBar ? undefined : (mobileRailOpen ? '100%' : rail.width) }}
        className={`flex-shrink-0 min-h-0 flex ${railBar ? 'w-full' : ''}`}
      >
        {rail.collapsed ? (
          railBar ? (
            // The bar carries the rail's OWN header — app mark, name, section nav
            // — plus the way back to the list. A bar holding nothing but an
            // expand glyph hid the app's entire navigation behind a control that
            // did not look like navigation: from a report there was no visible
            // route to Learning or Settings, and no labelled way back either.
            <div className="w-full flex flex-row items-center border-b border-border px-2 py-1.5 bg-bg-accent">
              {/* Re-tapping the ACTIVE section opens the rail rather than
                  re-setting the view it is already on. That tap did nothing at
                  all before — no state changed, so nothing rendered — on the one
                  control a user reaching for "the list" is most likely to press,
                  and it is also the tap-the-active-tab-to-pop-to-root convention.
                  Withheld where the rail holds no list, so it cannot open the
                  empty panel `backToList` is withheld to avoid. */}
              <RailHeader
                narrow
                leading={backToList}
                onReselect={canOpenList ? rail.expand : undefined}
              />
            </div>

          ) : (
            <div
              className="w-full flex flex-col items-center border-r border-border pt-2 bg-bg-accent"
            >
              <IconButton aria-label={i18nT('app.expand_sidebar')} onClick={rail.expand}>
                <ScanSearch size={16} className="text-accent" />
              </IconButton>
            </div>
          )
        ) : (
          // Narrow and open, the rail IS the page, so it needs its own exit —
          // see LeftRail's `onCollapse`.
          <LeftRail narrow={mobileRailOpen} onCollapse={rail.collapse} />
        )}
      </div>
      {/* The drag handle is a pointer affordance and costs width a phone does
          not have; the strip's expand button is the narrow-width control. */}
      {!isMobile && (
        <Splitter handleProps={rail.handleProps} label={i18nT('apps.codeReviewSage.workspace.resize_sidebar')} />
      )}

      {mainView === 'reviews' ? (
        <>

          {/* A flex COLUMN, not just a flex item: EmptyState (and any future
              child) sizes itself with flex-1, which is inert unless this element
              is itself a flex container — that bug left the empty state
              collapsed to content height and pinned to the top of the pane. */}
          <main className={mainClass}>
            {selectedPr ? (
              <PrReviewDetail pr={selectedPr} />
            ) : activeRun ? (
              <RunDetail run={activeRun} />
            ) : (
              <EmptyState
                icon={ScanSearch}
                title={i18nT('apps.codeReviewSage.workspace.select_a_review_to_see_its_progress_and_report')}
                hint={i18nT('apps.codeReviewSage.workspace.start_a_new_one_several_can_run_at_once')}
              >
                {/* The list this points at is on screen on a desktop and HIDDEN
                    behind the bar while narrow, so on a phone the copy asked the
                    user to select from something they could not see — and this is
                    the app's first-run mobile screen, where nothing is selected
                    yet. The same control the bar carries, so both routes to the
                    list read identically. */}
                {backToList}
              </EmptyState>

            )}
          </main>
        </>
      ) : mainView === 'local' ? (
        <main className={mainClass}>
          <Suspense fallback={<div className="flex-1" aria-busy="true" />}>
            <LocalReviewView />
          </Suspense>
        </main>
      ) : mainView === 'learning' ? (
        <main className={mainClass}><LearningView /></main>
      ) : (
        <main className={mainClass}><SettingsView /></main>
      )}
    </div>
  )
}
