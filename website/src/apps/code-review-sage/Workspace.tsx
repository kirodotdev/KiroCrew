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
import { useEffect } from 'react'

import { IconButton } from '../../components/ui'
import { type CollapseConfig, useColumnResize } from '../../hooks/useColumnResize'
import { useIsMobile } from '../../hooks/useIsMobile'
import EmptyState from './components/EmptyState'
import LeftRail from './components/LeftRail'
import PrReviewDetail from './components/PrReviewDetail'
import RunDetail from './components/RunDetail'
import { useSage } from './context'
import {
  COLLAPSED_RAIL_WIDTH, MAX_RAIL_WIDTH, MIN_RAIL_WIDTH, RAIL_COLLAPSED_KEY, RAIL_WIDTH_KEY,
  loadRailCollapsed, loadRailWidth,
} from './lib/layout'
import LearningView from './views/LearningView'
import SettingsView from './views/SettingsView'

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

export default function Workspace() {
  const { mainView, activeRun, selectedPr } = useSage()

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
          <div
            className={`w-full flex ${railBar
              ? 'flex-row items-center border-b border-border px-2 py-1.5'
              : 'flex-col items-center border-r border-border pt-2'} bg-bg-accent`}
          >
            <IconButton aria-label={i18nT('app.expand_sidebar')} onClick={rail.expand}>
              <ScanSearch size={16} className="text-accent" />
            </IconButton>
          </div>
        ) : (
          <LeftRail />
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
              />
            )}
          </main>
        </>
      ) : mainView === 'learning' ? (
        <main className={mainClass}><LearningView /></main>
      ) : (
        <main className={mainClass}><SettingsView /></main>
      )}
    </div>
  )
}
